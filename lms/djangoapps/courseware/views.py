"""
Courseware views functions
"""

import logging
import urllib
import json
import cgi

from collections import OrderedDict
from datetime import datetime
from django.utils import translation
from django.utils.translation import ugettext as _
from django.utils.translation import ungettext

from django.conf import settings
from django.core.context_processors import csrf
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User, AnonymousUser
from django.contrib.auth.decorators import login_required
from django.utils.timezone import UTC
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from certificates import api as certs_api
from edxmako.shortcuts import render_to_response, render_to_string, marketing_link
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.cache import cache_control
from django.db import transaction
from markupsafe import escape

from courseware import grades
from courseware.access import has_access, in_preview_mode, _adjust_start_date_for_beta_testers
from courseware.courses import (
    get_courses, get_course,
    get_studio_url, get_course_with_access,
    sort_by_announcement,
    sort_by_start_date,
)
from courseware.masquerade import setup_masquerade
from openedx.core.djangoapps.credit.api import (
    get_credit_requirement_status,
    is_user_eligible_for_credit,
    is_credit_course
)
from courseware.model_data import FieldDataCache
from .module_render import toc_for_course, get_module_for_descriptor, get_module, get_module_by_usage_id
from .entrance_exams import (
    course_has_entrance_exam,
    get_entrance_exam_content,
    get_entrance_exam_score,
    user_must_complete_entrance_exam,
    user_has_passed_entrance_exam
)
from courseware.user_state_client import DjangoXBlockUserStateClient
from course_modes.models import CourseMode

from open_ended_grading import open_ended_notifications
from open_ended_grading.views import StaffGradingTab, PeerGradingTab, OpenEndedGradingTab
from student.models import UserTestGroup, CourseEnrollment
from student.views import is_course_blocked
from util.cache import cache, cache_if_anonymous
from xblock.fragment import Fragment
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.exceptions import ItemNotFoundError, NoPathToItem
from xmodule.tabs import CourseTabList
from xmodule.x_module import STUDENT_VIEW
import shoppingcart
from shoppingcart.models import CourseRegistrationCode
from shoppingcart.utils import is_shopping_cart_enabled
from opaque_keys import InvalidKeyError
from util.milestones_helpers import get_prerequisite_courses_display

from microsite_configuration import microsite
from opaque_keys.edx.locations import SlashSeparatedCourseKey
from opaque_keys.edx.keys import CourseKey, UsageKey
from instructor.enrollment import uses_shib

from util.db import commit_on_success_with_read_committed

import survey.utils
import survey.views

from util.views import ensure_valid_course_key
from eventtracking import tracker
import analytics
from courseware.url_helpers import get_redirect_url

log = logging.getLogger("edx.courseware")

template_imports = {'urllib': urllib}

CONTENT_DEPTH = 2


def user_groups(user):
    """
    TODO (vshnayder): This is not used. When we have a new plan for groups, adjust appropriately.
    """
    if not user.is_authenticated():
        return []

    # TODO: Rewrite in Django
    key = 'user_group_names_{user.id}'.format(user=user)
    cache_expiration = 60 * 60  # one hour

    # Kill caching on dev machines -- we switch groups a lot
    group_names = cache.get(key)
    if settings.DEBUG:
        group_names = None

    if group_names is None:
        group_names = [u.name for u in UserTestGroup.objects.filter(users=user)]
        cache.set(key, group_names, cache_expiration)

    return group_names


@ensure_csrf_cookie
@cache_if_anonymous()
def courses(request):
    """
    Render "find courses" page.  The course selection work is done in courseware.courses.
    """
    courses_list = []
    course_discovery_meanings = getattr(settings, 'COURSE_DISCOVERY_MEANINGS', {})
    if not settings.FEATURES.get('ENABLE_COURSE_DISCOVERY'):
        courses_list = get_courses(request.user, request.META.get('HTTP_HOST'))

        if microsite.get_value("ENABLE_COURSE_SORTING_BY_START_DATE",
                               settings.FEATURES["ENABLE_COURSE_SORTING_BY_START_DATE"]):
            courses_list = sort_by_start_date(courses_list)
        else:
            courses_list = sort_by_announcement(courses_list)

    return render_to_response(
        "courseware/courses.html",
        {'courses': courses_list, 'course_discovery_meanings': course_discovery_meanings}
    )


def render_accordion(request, course, chapter, section, field_data_cache):
    """
    Draws navigation bar. Takes current position in accordion as
    parameter.

    If chapter and section are '' or None, renders a default accordion.

    course, chapter, and section are the url_names.

    Returns the html string
    """
    # grab the table of contents
    toc = toc_for_course(request, course, chapter, section, field_data_cache)

    context = dict([
        ('toc', toc),
        ('course_id', course.id.to_deprecated_string()),
        ('csrf', csrf(request)['csrf_token']),
        ('due_date_display_format', course.due_date_display_format)
    ] + template_imports.items())
    return render_to_string('courseware/accordion.html', context)


def get_current_child(xmodule, min_depth=None):
    """
    Get the xmodule.position's display item of an xmodule that has a position and
    children.  If xmodule has no position or is out of bounds, return the first
    child with children extending down to content_depth.

    For example, if chapter_one has no position set, with two child sections,
    section-A having no children and section-B having a discussion unit,
    `get_current_child(chapter, min_depth=1)`  will return section-B.

    Returns None only if there are no children at all.
    """
    def _get_default_child_module(child_modules):
        """Returns the first child of xmodule, subject to min_depth."""
        if not child_modules:
            default_child = None
        elif not min_depth > 0:
            default_child = child_modules[0]
        else:
            content_children = [child for child in child_modules if
                                child.has_children_at_depth(min_depth - 1) and child.get_display_items()]
            default_child = content_children[0] if content_children else None

        return default_child

    if not hasattr(xmodule, 'position'):
        return None

    if xmodule.position is None:
        return _get_default_child_module(xmodule.get_display_items())
    else:
        # position is 1-indexed.
        pos = xmodule.position - 1

    children = xmodule.get_display_items()
    if 0 <= pos < len(children):
        child = children[pos]
    elif len(children) > 0:
        # module has a set position, but the position is out of range.
        # return default child.
        child = _get_default_child_module(children)
    else:
        child = None
    return child


def redirect_to_course_position(course_module, content_depth):
    """
    Return a redirect to the user's current place in the course.

    If this is the user's first time, redirects to COURSE/CHAPTER/SECTION.
    If this isn't the users's first time, redirects to COURSE/CHAPTER,
    and the view will find the current section and display a message
    about reusing the stored position.

    If there is no current position in the course or chapter, then selects
    the first child.

    """
    urlargs = {'course_id': course_module.id.to_deprecated_string()}
    chapter = get_current_child(course_module, min_depth=content_depth)
    if chapter is None:
        # oops.  Something bad has happened.
        raise Http404("No chapter found when loading current position in course")

    urlargs['chapter'] = chapter.url_name
    if course_module.position is not None:
        return redirect(reverse('courseware_chapter', kwargs=urlargs))

    # Relying on default of returning first child
    section = get_current_child(chapter, min_depth=content_depth - 1)
    if section is None:
        raise Http404("No section found when loading current position in course")

    urlargs['section'] = section.url_name
    return redirect(reverse('courseware_section', kwargs=urlargs))


def save_child_position(seq_module, child_name):
    """
    child_name: url_name of the child
    """
    for position, c in enumerate(seq_module.get_display_items(), start=1):
        if c.location.name == child_name:
            # Only save if position changed
            if position != seq_module.position:
                seq_module.position = position
    # Save this new position to the underlying KeyValueStore
    seq_module.save()


def save_positions_recursively_up(user, request, field_data_cache, xmodule, course=None):
    """
    Recurses up the course tree starting from a leaf
    Saving the position property based on the previous node as it goes
    """
    current_module = xmodule

    while current_module:
        parent_location = modulestore().get_parent_location(current_module.location)
        parent = None
        if parent_location:
            parent_descriptor = modulestore().get_item(parent_location)
            parent = get_module_for_descriptor(
                user,
                request,
                parent_descriptor,
                field_data_cache,
                current_module.location.course_key,
                course=course
            )

        if parent and hasattr(parent, 'position'):
            save_child_position(parent, current_module.location.name)

        current_module = parent


def chat_settings(course, user):
    """
    Returns a dict containing the settings required to connect to a
    Jabber chat server and room.
    """
    domain = getattr(settings, "JABBER_DOMAIN", None)
    if domain is None:
        log.warning('You must set JABBER_DOMAIN in the settings to '
                    'enable the chat widget')
        return None

    return {
        'domain': domain,

        # Jabber doesn't like slashes, so replace with dashes
        'room': "{ID}_class".format(ID=course.id.replace('/', '-')),

        'username': "{USER}@{DOMAIN}".format(
            USER=user.username, DOMAIN=domain
        ),

        # TODO: clearly this needs to be something other than the username
        #       should also be something that's not necessarily tied to a
        #       particular course
        'password': "{USER}@{DOMAIN}".format(
            USER=user.username, DOMAIN=domain
        ),
    }


@login_required
@ensure_csrf_cookie
@cache_control(no_cache=True, no_store=True, must_revalidate=True)
@ensure_valid_course_key
@commit_on_success_with_read_committed
def index(request, course_id, chapter=None, section=None,
          position=None):
    """
    Displays courseware accordion and associated content.  If course, chapter,
    and section are all specified, renders the page, or returns an error if they
    are invalid.

    If section is not specified, displays the accordion opened to the right chapter.

    If neither chapter or section are specified, redirects to user's most recent
    chapter, or the first chapter if this is the user's first visit.

    Arguments:

     - request    : HTTP request
     - course_id  : course id (str: ORG/course/URL_NAME)
     - chapter    : chapter url_name (str)
     - section    : section url_name (str)
     - position   : position in module, eg of <sequential> module (str)

    Returns:

     - HTTPresponse
    """

    course_key = CourseKey.from_string(course_id)

    user = User.objects.prefetch_related("groups").get(id=request.user.id)

    redeemed_registration_codes = CourseRegistrationCode.objects.filter(
        course_id=course_key,
        registrationcoderedemption__redeemed_by=request.user
    )

    # Redirect to dashboard if the course is blocked due to non-payment.
    if is_course_blocked(request, redeemed_registration_codes, course_key):
        # registration codes may be generated via Bulk Purchase Scenario
        # we have to check only for the invoice generated registration codes
        # that their invoice is valid or not
        log.warning(
            u'User %s cannot access the course %s because payment has not yet been received',
            user,
            course_key.to_deprecated_string()
        )
        return redirect(reverse('dashboard'))

    request.user = user  # keep just one instance of User
    with modulestore().bulk_operations(course_key):
        return _index_bulk_op(request, course_key, chapter, section, position)


# pylint: disable=too-many-statements
def _index_bulk_op(request, course_key, chapter, section, position):
    """
    Render the index page for the specified course.
    """
    # Verify that position a string is in fact an int
    if position is not None:
        try:
            int(position)
        except ValueError:
            raise Http404(u"Position {} is not an integer!".format(position))

    user = request.user
    course = get_course_with_access(user, 'load', course_key, depth=2)

    staff_access = has_access(user, 'staff', course)
    registered = registered_for_course(course, user)
    if not registered:
        # TODO (vshnayder): do course instructors need to be registered to see course?
        log.debug(u'User %s tried to view course %s but is not enrolled', user, course.location.to_deprecated_string())
        return redirect(reverse('about_course', args=[course_key.to_deprecated_string()]))

    # see if all pre-requisites (as per the milestones app feature) have been fulfilled
    # Note that if the pre-requisite feature flag has been turned off (default) then this check will
    # always pass
    if not has_access(user, 'view_courseware_with_prerequisites', course):
        # prerequisites have not been fulfilled therefore redirect to the Dashboard
        log.info(
            u'User %d tried to view course %s '
            u'without fulfilling prerequisites',
            user.id, unicode(course.id))
        return redirect(reverse('dashboard'))

    # Entrance Exam Check
    # If the course has an entrance exam and the requested chapter is NOT the entrance exam, and
    # the user hasn't yet met the criteria to bypass the entrance exam, redirect them to the exam.
    if chapter and course_has_entrance_exam(course):
        chapter_descriptor = course.get_child_by(lambda m: m.location.name == chapter)
        if chapter_descriptor and not getattr(chapter_descriptor, 'is_entrance_exam', False) \
                and user_must_complete_entrance_exam(request, user, course):
            log.info(u'User %d tried to view course %s without passing entrance exam', user.id, unicode(course.id))
            return redirect(reverse('courseware', args=[unicode(course.id)]))
    # check to see if there is a required survey that must be taken before
    # the user can access the course.
    if survey.utils.must_answer_survey(course, user):
        return redirect(reverse('course_survey', args=[unicode(course.id)]))

    masquerade = setup_masquerade(request, course_key, staff_access)

    try:
        field_data_cache = FieldDataCache.cache_for_descriptor_descendents(
            course_key, user, course, depth=2)

        course_module = get_module_for_descriptor(
            user, request, course, field_data_cache, course_key, course=course
        )
        if course_module is None:
            log.warning(u'If you see this, something went wrong: if we got this'
                        u' far, should have gotten a course module for this user')
            return redirect(reverse('about_course', args=[course_key.to_deprecated_string()]))

        studio_url = get_studio_url(course, 'course')

        context = {
            'csrf': csrf(request)['csrf_token'],
            'accordion': render_accordion(request, course, chapter, section, field_data_cache),
            'COURSE_TITLE': course.display_name_with_default,
            'course': course,
            'init': '',
            'fragment': Fragment(),
            'staff_access': staff_access,
            'studio_url': studio_url,
            'masquerade': masquerade,
            'xqa_server': settings.FEATURES.get('XQA_SERVER', "http://your_xqa_server.com"),
        }

        now = datetime.now(UTC())
        effective_start = _adjust_start_date_for_beta_testers(user, course, course_key)
        if not in_preview_mode() and staff_access and now < effective_start:
            # Disable student view button if user is staff and
            # course is not yet visible to students.
            context['disable_student_access'] = True

        has_content = course.has_children_at_depth(CONTENT_DEPTH)
        if not has_content:
            # Show empty courseware for a course with no units
            return render_to_response('courseware/courseware.html', context)
        elif chapter is None:
            # Check first to see if we should instead redirect the user to an Entrance Exam
            if course_has_entrance_exam(course):
                exam_chapter = get_entrance_exam_content(request, course)
                if exam_chapter:
                    exam_section = None
                    if exam_chapter.get_children():
                        exam_section = exam_chapter.get_children()[0]
                        if exam_section:
                            return redirect('courseware_section',
                                            course_id=unicode(course_key),
                                            chapter=exam_chapter.url_name,
                                            section=exam_section.url_name)

            # passing CONTENT_DEPTH avoids returning 404 for a course with an
            # empty first section and a second section with content
            return redirect_to_course_position(course_module, CONTENT_DEPTH)

        # Only show the chat if it's enabled by the course and in the
        # settings.
        show_chat = course.show_chat and settings.FEATURES['ENABLE_CHAT']
        if show_chat:
            context['chat'] = chat_settings(course, user)
            # If we couldn't load the chat settings, then don't show
            # the widget in the courseware.
            if context['chat'] is None:
                show_chat = False

        context['show_chat'] = show_chat

        chapter_descriptor = course.get_child_by(lambda m: m.location.name == chapter)
        if chapter_descriptor is not None:
            save_child_position(course_module, chapter)
        else:
            raise Http404('No chapter descriptor found with name {}'.format(chapter))

        chapter_module = course_module.get_child_by(lambda m: m.location.name == chapter)
        if chapter_module is None:
            # User may be trying to access a chapter that isn't live yet
            if masquerade and masquerade.role == 'student':  # if staff is masquerading as student be kinder, don't 404
                log.debug('staff masquerading as student: no chapter %s', chapter)
                return redirect(reverse('courseware', args=[course.id.to_deprecated_string()]))
            raise Http404

        if course_has_entrance_exam(course):
            # Message should not appear outside the context of entrance exam subsection.
            # if section is none then we don't need to show message on welcome back screen also.
            if getattr(chapter_module, 'is_entrance_exam', False) and section is not None:
                context['entrance_exam_current_score'] = get_entrance_exam_score(request, course)
                context['entrance_exam_passed'] = user_has_passed_entrance_exam(request, course)

        if section is not None:
            section_descriptor = chapter_descriptor.get_child_by(lambda m: m.location.name == section)

            if section_descriptor is None:
                # Specifically asked-for section doesn't exist
                if masquerade and masquerade.role == 'student':  # don't 404 if staff is masquerading as student
                    log.debug('staff masquerading as student: no section %s', section)
                    return redirect(reverse('courseware', args=[course.id.to_deprecated_string()]))
                raise Http404

            ## Allow chromeless operation
            if section_descriptor.chrome:
                chrome = [s.strip() for s in section_descriptor.chrome.lower().split(",")]
                if 'accordion' not in chrome:
                    context['disable_accordion'] = True
                if 'tabs' not in chrome:
                    context['disable_tabs'] = True

            if section_descriptor.default_tab:
                context['default_tab'] = section_descriptor.default_tab

            # cdodge: this looks silly, but let's refetch the section_descriptor with depth=None
            # which will prefetch the children more efficiently than doing a recursive load
            section_descriptor = modulestore().get_item(section_descriptor.location, depth=None)

            # Load all descendants of the section, because we're going to display its
            # html, which in general will need all of its children
            field_data_cache.add_descriptor_descendents(
                section_descriptor, depth=None
            )

            section_module = get_module_for_descriptor(
                request.user,
                request,
                section_descriptor,
                field_data_cache,
                course_key,
                position,
                course=course
            )

            if section_module is None:
                # User may be trying to be clever and access something
                # they don't have access to.
                raise Http404

            # Save where we are in the chapter
            save_child_position(chapter_module, section)
            context['fragment'] = section_module.render(STUDENT_VIEW)
            context['section_title'] = section_descriptor.display_name_with_default
        else:
            # section is none, so display a message
            studio_url = get_studio_url(course, 'course')
            prev_section = get_current_child(chapter_module)
            if prev_section is None:
                # Something went wrong -- perhaps this chapter has no sections visible to the user.
                # Clearing out the last-visited state and showing "first-time" view by redirecting
                # to courseware.
                course_module.position = None
                course_module.save()
                return redirect(reverse('courseware', args=[course.id.to_deprecated_string()]))
            prev_section_url = reverse('courseware_section', kwargs={
                'course_id': course_key.to_deprecated_string(),
                'chapter': chapter_descriptor.url_name,
                'section': prev_section.url_name
            })
            context['fragment'] = Fragment(content=render_to_string(
                'courseware/welcome-back.html',
                {
                    'course': course,
                    'studio_url': studio_url,
                    'chapter_module': chapter_module,
                    'prev_section': prev_section,
                    'prev_section_url': prev_section_url
                }
            ))

        result = render_to_response('courseware/courseware.html', context)
    except Exception as e:

        # Doesn't bar Unicode characters from URL, but if Unicode characters do
        # cause an error it is a graceful failure.
        if isinstance(e, UnicodeEncodeError):
            raise Http404("URL contains Unicode characters")

        if isinstance(e, Http404):
            # let it propagate
            raise

        # In production, don't want to let a 500 out for any reason
        if settings.DEBUG:
            raise
        else:
            log.exception(
                u"Error in index view: user={user}, course={course}, chapter={chapter}"
                u" section={section} position={position}".format(
                    user=user,
                    course=course,
                    chapter=chapter,
                    section=section,
                    position=position
                ))
            try:
                result = render_to_response('courseware/courseware-error.html', {
                    'staff_access': staff_access,
                    'course': course
                })
            except:
                # Let the exception propagate, relying on global config to at
                # at least return a nice error message
                log.exception("Error while rendering courseware-error page")
                raise

    return result


@ensure_csrf_cookie
@ensure_valid_course_key
def jump_to_id(request, course_id, module_id):
    """
    This entry point allows for a shorter version of a jump to where just the id of the element is
    passed in. This assumes that id is unique within the course_id namespace
    """
    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)
    items = modulestore().get_items(course_key, qualifiers={'name': module_id})

    if len(items) == 0:
        raise Http404(
            u"Could not find id: {0} in course_id: {1}. Referer: {2}".format(
                module_id, course_id, request.META.get("HTTP_REFERER", "")
            ))
    if len(items) > 1:
        log.warning(
            u"Multiple items found with id: {0} in course_id: {1}. Referer: {2}. Using first: {3}".format(
                module_id, course_id, request.META.get("HTTP_REFERER", ""), items[0].location.to_deprecated_string()
            ))

    return jump_to(request, course_id, items[0].location.to_deprecated_string())


@ensure_csrf_cookie
def jump_to(_request, course_id, location):
    """
    Show the page that contains a specific location.

    If the location is invalid or not in any class, return a 404.

    Otherwise, delegates to the index view to figure out whether this user
    has access, and what they should see.
    """
    try:
        course_key = CourseKey.from_string(course_id)
        usage_key = UsageKey.from_string(location).replace(course_key=course_key)
    except InvalidKeyError:
        raise Http404(u"Invalid course_key or usage_key")
    try:
        redirect_url = get_redirect_url(course_key, usage_key)
    except ItemNotFoundError:
        raise Http404(u"No data at this location: {0}".format(usage_key))
    except NoPathToItem:
        raise Http404(u"This location is not in any class: {0}".format(usage_key))

    return redirect(redirect_url)


@ensure_csrf_cookie
@ensure_valid_course_key
def course_info(request, course_id):
    """
    Display the course's info.html, or 404 if there is no such course.

    Assumes the course_id is in a valid format.
    """
    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    with modulestore().bulk_operations(course_key):
        course = get_course_with_access(request.user, 'load', course_key)

        # If the user needs to take an entrance exam to access this course, then we'll need
        # to send them to that specific course module before allowing them into other areas
        if user_must_complete_entrance_exam(request, request.user, course):
            return redirect(reverse('courseware', args=[unicode(course.id)]))

        # check to see if there is a required survey that must be taken before
        # the user can access the course.
        if request.user.is_authenticated() and survey.utils.must_answer_survey(course, request.user):
            return redirect(reverse('course_survey', args=[unicode(course.id)]))

        staff_access = has_access(request.user, 'staff', course)
        masquerade = setup_masquerade(request, course_key, staff_access)  # allow staff to masquerade on the info page
        studio_url = get_studio_url(course, 'course_info')

        # link to where the student should go to enroll in the course:
        # about page if there is not marketing site, SITE_NAME if there is
        url_to_enroll = reverse(course_about, args=[course_id])
        if settings.FEATURES.get('ENABLE_MKTG_SITE'):
            url_to_enroll = marketing_link('COURSES')

        show_enroll_banner = request.user.is_authenticated() and not CourseEnrollment.is_enrolled(request.user, course.id)

        context = {
            'request': request,
            'course_id': course_key.to_deprecated_string(),
            'cache': None,
            'course': course,
            'staff_access': staff_access,
            'masquerade': masquerade,
            'studio_url': studio_url,
            'show_enroll_banner': show_enroll_banner,
            'url_to_enroll': url_to_enroll,
        }

        now = datetime.now(UTC())
        effective_start = _adjust_start_date_for_beta_testers(request.user, course, course_key)
        if not in_preview_mode() and staff_access and now < effective_start:
            # Disable student view button if user is staff and
            # course is not yet visible to students.
            context['disable_student_access'] = True

        return render_to_response('courseware/info.html', context)


@ensure_csrf_cookie
@ensure_valid_course_key
def static_tab(request, course_id, tab_slug):
    """
    Display the courses tab with the given name.

    Assumes the course_id is in a valid format.
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    course = get_course_with_access(request.user, 'load', course_key)

    tab = CourseTabList.get_tab_by_slug(course.tabs, tab_slug)
    if tab is None:
        raise Http404

    contents = get_static_tab_contents(
        request,
        course,
        tab
    )
    if contents is None:
        raise Http404

    return render_to_response('courseware/static_tab.html', {
        'course': course,
        'tab': tab,
        'tab_contents': contents,
    })


@ensure_csrf_cookie
@ensure_valid_course_key
def syllabus(request, course_id):
    """
    Display the course's syllabus.html, or 404 if there is no such course.

    Assumes the course_id is in a valid format.
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    course = get_course_with_access(request.user, 'load', course_key)
    staff_access = has_access(request.user, 'staff', course)

    return render_to_response('courseware/syllabus.html', {
        'course': course,
        'staff_access': staff_access,
    })


def registered_for_course(course, user):
    """
    Return True if user is registered for course, else False
    """
    if user is None:
        return False
    if user.is_authenticated():
        return CourseEnrollment.is_enrolled(user, course.id)
    else:
        return False


def get_cosmetic_display_price(course, registration_price):
    """
    Return Course Price as a string preceded by correct currency, or 'Free'
    """
    currency_symbol = settings.PAID_COURSE_REGISTRATION_CURRENCY[1]

    price = course.cosmetic_display_price
    if registration_price > 0:
        price = registration_price

    if price:
        # Translators: This will look like '$50', where {currency_symbol} is a symbol such as '$' and {price} is a
        # numerical amount in that currency. Adjust this display as needed for your language.
        return _("{currency_symbol}{price}").format(currency_symbol=currency_symbol, price=price)
    else:
        # Translators: This refers to the cost of the course. In this case, the course costs nothing so it is free.
        return _('Free')


@ensure_csrf_cookie
@cache_if_anonymous()
def course_about(request, course_id):
    """
    Display the course's about page.

    Assumes the course_id is in a valid format.
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    with modulestore().bulk_operations(course_key):
        permission_name = microsite.get_value(
            'COURSE_ABOUT_VISIBILITY_PERMISSION',
            settings.COURSE_ABOUT_VISIBILITY_PERMISSION
        )
        course = get_course_with_access(request.user, permission_name, course_key)

        if microsite.get_value('ENABLE_MKTG_SITE', settings.FEATURES.get('ENABLE_MKTG_SITE', False)):
            return redirect(reverse('info', args=[course.id.to_deprecated_string()]))

        registered = registered_for_course(course, request.user)

        staff_access = has_access(request.user, 'staff', course)
        studio_url = get_studio_url(course, 'settings/details')

        if has_access(request.user, 'load', course):
            course_target = reverse('info', args=[course.id.to_deprecated_string()])
        else:
            course_target = reverse('about_course', args=[course.id.to_deprecated_string()])

        show_courseware_link = (
            (
                has_access(request.user, 'load', course)
                and has_access(request.user, 'view_courseware_with_prerequisites', course)
            )
            or settings.FEATURES.get('ENABLE_LMS_MIGRATION')
        )

        # Note: this is a flow for payment for course registration, not the Verified Certificate flow.
        registration_price = 0
        in_cart = False
        reg_then_add_to_cart_link = ""

        _is_shopping_cart_enabled = is_shopping_cart_enabled()
        if _is_shopping_cart_enabled:
            registration_price = CourseMode.min_course_price_for_currency(course_key,
                                                                          settings.PAID_COURSE_REGISTRATION_CURRENCY[0])
            if request.user.is_authenticated():
                cart = shoppingcart.models.Order.get_cart_for_user(request.user)
                in_cart = shoppingcart.models.PaidCourseRegistration.contained_in_order(cart, course_key) or \
                    shoppingcart.models.CourseRegCodeItem.contained_in_order(cart, course_key)

            reg_then_add_to_cart_link = "{reg_url}?course_id={course_id}&enrollment_action=add_to_cart".format(
                reg_url=reverse('register_user'), course_id=urllib.quote(str(course_id)))

        course_price = get_cosmetic_display_price(course, registration_price)
        can_add_course_to_cart = _is_shopping_cart_enabled and registration_price

        # Used to provide context to message to student if enrollment not allowed
        can_enroll = has_access(request.user, 'enroll', course)
        invitation_only = course.invitation_only
        is_course_full = CourseEnrollment.objects.is_course_full(course)

        # Register button should be disabled if one of the following is true:
        # - Student is already registered for course
        # - Course is already full
        # - Student cannot enroll in course
        active_reg_button = not(registered or is_course_full or not can_enroll)

        is_shib_course = uses_shib(course)

        # get prerequisite courses display names
        pre_requisite_courses = get_prerequisite_courses_display(course)

        return render_to_response('courseware/course_about.html', {
            'course': course,
            'staff_access': staff_access,
            'studio_url': studio_url,
            'registered': registered,
            'course_target': course_target,
            'is_cosmetic_price_enabled': settings.FEATURES.get('ENABLE_COSMETIC_DISPLAY_PRICE'),
            'course_price': course_price,
            'in_cart': in_cart,
            'reg_then_add_to_cart_link': reg_then_add_to_cart_link,
            'show_courseware_link': show_courseware_link,
            'is_course_full': is_course_full,
            'can_enroll': can_enroll,
            'invitation_only': invitation_only,
            'active_reg_button': active_reg_button,
            'is_shib_course': is_shib_course,
            # We do not want to display the internal courseware header, which is used when the course is found in the
            # context. This value is therefor explicitly set to render the appropriate header.
            'disable_courseware_header': True,
            'can_add_course_to_cart': can_add_course_to_cart,
            'cart_link': reverse('shoppingcart.views.show_cart'),
            'pre_requisite_courses': pre_requisite_courses
        })


@ensure_csrf_cookie
@cache_if_anonymous('org')
@ensure_valid_course_key
def mktg_course_about(request, course_id):
    """This is the button that gets put into an iframe on the Drupal site."""
    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    try:
        permission_name = microsite.get_value(
            'COURSE_ABOUT_VISIBILITY_PERMISSION',
            settings.COURSE_ABOUT_VISIBILITY_PERMISSION
        )
        course = get_course_with_access(request.user, permission_name, course_key)
    except (ValueError, Http404):
        # If a course does not exist yet, display a "Coming Soon" button
        return render_to_response(
            'courseware/mktg_coming_soon.html', {'course_id': course_key.to_deprecated_string()}
        )

    registered = registered_for_course(course, request.user)

    if has_access(request.user, 'load', course):
        course_target = reverse('info', args=[course.id.to_deprecated_string()])
    else:
        course_target = reverse('about_course', args=[course.id.to_deprecated_string()])

    allow_registration = has_access(request.user, 'enroll', course)

    show_courseware_link = (has_access(request.user, 'load', course) or
                            settings.FEATURES.get('ENABLE_LMS_MIGRATION'))
    course_modes = CourseMode.modes_for_course_dict(course.id)

    context = {
        'course': course,
        'registered': registered,
        'allow_registration': allow_registration,
        'course_target': course_target,
        'show_courseware_link': show_courseware_link,
        'course_modes': course_modes,
    }

    # The edx.org marketing site currently displays only in English.
    # To avoid displaying a different language in the register / access button,
    # we force the language to English.
    # However, OpenEdX installations with a different marketing front-end
    # may want to respect the language specified by the user or the site settings.
    force_english = settings.FEATURES.get('IS_EDX_DOMAIN', False)
    if force_english:
        translation.activate('en-us')

    if settings.FEATURES.get('ENABLE_MKTG_EMAIL_OPT_IN'):
        # Drupal will pass organization names using a GET parameter, as follows:
        #     ?org=Harvard
        #     ?org=Harvard,MIT
        # If no full names are provided, the marketing iframe won't show the
        # email opt-in checkbox.
        org = request.GET.get('org')
        if org:
            org_list = org.split(',')
            # HTML-escape the provided organization names
            org_list = [cgi.escape(org) for org in org_list]
            if len(org_list) > 1:
                if len(org_list) > 2:
                    # Translators: The join of three or more institution names (e.g., Harvard, MIT, and Dartmouth).
                    org_name_string = _("{first_institutions}, and {last_institution}").format(
                        first_institutions=u", ".join(org_list[:-1]),
                        last_institution=org_list[-1]
                    )
                else:
                    # Translators: The join of two institution names (e.g., Harvard and MIT).
                    org_name_string = _("{first_institution} and {second_institution}").format(
                        first_institution=org_list[0],
                        second_institution=org_list[1]
                    )
            else:
                org_name_string = org_list[0]

            context['checkbox_label'] = ungettext(
                "I would like to receive email from {institution_series} and learn about its other programs.",
                "I would like to receive email from {institution_series} and learn about their other programs.",
                len(org_list)
            ).format(institution_series=org_name_string)

    try:
        return render_to_response('courseware/mktg_course_about.html', context)
    finally:
        # Just to be safe, reset the language if we forced it to be English.
        if force_english:
            translation.deactivate()


@login_required
@cache_control(no_cache=True, no_store=True, must_revalidate=True)
@transaction.commit_manually
@ensure_valid_course_key
def progress(request, course_id, student_id=None):
    """
    Wraps "_progress" with the manual_transaction context manager just in case
    there are unanticipated errors.
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    with modulestore().bulk_operations(course_key):
        with grades.manual_transaction():
            return _progress(request, course_key, student_id)


def _progress(request, course_key, student_id):
    """
    Unwrapped version of "progress".

    User progress. We show the grade bar and every problem score.

    Course staff are allowed to see the progress of students in their class.
    """
    course = get_course_with_access(request.user, 'load', course_key, depth=None, check_if_enrolled=True)

    # check to see if there is a required survey that must be taken before
    # the user can access the course.
    if survey.utils.must_answer_survey(course, request.user):
        return redirect(reverse('course_survey', args=[unicode(course.id)]))

    staff_access = has_access(request.user, 'staff', course)

    if student_id is None or student_id == request.user.id:
        # always allowed to see your own profile
        student = request.user
    else:
        # Requesting access to a different student's profile
        if not staff_access:
            raise Http404
        try:
            student = User.objects.get(id=student_id)
        # Check for ValueError if 'student_id' cannot be converted to integer.
        except (ValueError, User.DoesNotExist):
            raise Http404

    # NOTE: To make sure impersonation by instructor works, use
    # student instead of request.user in the rest of the function.

    # The pre-fetching of groups is done to make auth checks not require an
    # additional DB lookup (this kills the Progress page in particular).
    student = User.objects.prefetch_related("groups").get(id=student.id)

    courseware_summary = grades.progress_summary(student, request, course)
    studio_url = get_studio_url(course, 'settings/grading')
    grade_summary = grades.grade(student, request, course)

    if courseware_summary is None:
        #This means the student didn't have access to the course (which the instructor requested)
        raise Http404

    # checking certificate generation configuration
    show_generate_cert_btn = certs_api.cert_generation_enabled(course_key)

    context = {
        'course': course,
        'courseware_summary': courseware_summary,
        'studio_url': studio_url,
        'grade_summary': grade_summary,
        'staff_access': staff_access,
        'student': student,
        'passed': is_course_passed(course, grade_summary),
        'show_generate_cert_btn': show_generate_cert_btn,
        'credit_course_requirements': _credit_course_requirements(course_key, student),
    }

    if show_generate_cert_btn:
        context.update(certs_api.certificate_downloadable_status(student, course_key))
        # showing the certificate web view button if feature flags are enabled.
        if certs_api.has_html_certificates_enabled(course_key, course):
            if certs_api.get_active_web_certificate(course) is not None:
                context.update({
                    'show_cert_web_view': True,
                    'cert_web_view_url': u'{url}'.format(
                        url=certs_api.get_certificate_url(
                            user_id=student.id,
                            course_id=unicode(course.id)
                        )
                    )
                })
            else:
                context.update({
                    'is_downloadable': False,
                    'is_generating': True,
                    'download_url': None
                })

    with grades.manual_transaction():
        response = render_to_response('courseware/progress.html', context)

    return response


def _credit_course_requirements(course_key, student):
    """Return information about which credit requirements a user has satisfied.

    Arguments:
        course_key (CourseKey): Identifier for the course.
        student (User): Currently logged in user.

    Returns: dict

    """
    # If credit eligibility is not enabled or this is not a credit course,
    # short-circuit and return `None`.  This indicates that credit requirements
    # should NOT be displayed on the progress page.
    if not (settings.FEATURES.get("ENABLE_CREDIT_ELIGIBILITY", False) and is_credit_course(course_key)):
        return None

    # Retrieve the status of the user for each eligibility requirement in the course.
    # For each requirement, the user's status is either "satisfied", "failed", or None.
    # In this context, `None` means that we don't know the user's status, either because
    # the user hasn't done something (for example, submitting photos for verification)
    # or we're waiting on more information (for example, a response from the photo
    # verification service).
    requirement_statuses = get_credit_requirement_status(course_key, student.username)

    # If the user has been marked as "eligible", then they are *always* eligible
    # unless someone manually intervenes.  This could lead to some strange behavior
    # if the requirements change post-launch.  For example, if the user was marked as eligible
    # for credit, then a new requirement was added, the user will see that they're eligible
    # AND that one of the requirements is still pending.
    # We're assuming here that (a) we can mitigate this by properly training course teams,
    # and (b) it's a better user experience to allow students who were at one time
    # marked as eligible to continue to be eligible.
    # If we need to, we can always manually move students back to ineligible by
    # deleting CreditEligibility records in the database.
    if is_user_eligible_for_credit(student.username, course_key):
        eligibility_status = "eligible"

    # If the user has *failed* any requirements (for example, if a photo verification is denied),
    # then the user is NOT eligible for credit.
    elif any(requirement['status'] == 'failed' for requirement in requirement_statuses):
        eligibility_status = "not_eligible"

    # Otherwise, the user may be eligible for credit, but the user has not
    # yet completed all the requirements.
    else:
        eligibility_status = "partial_eligible"

    return {
        'eligibility_status': eligibility_status,
        'requirements': requirement_statuses,
    }


@login_required
@ensure_valid_course_key
def submission_history(request, course_id, student_username, location):
    """Render an HTML fragment (meant for inclusion elsewhere) that renders a
    history of all state changes made by this user for this problem location.
    Right now this only works for problems because that's all
    StudentModuleHistory records.
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    try:
        usage_key = course_key.make_usage_key_from_deprecated_string(location)
    except (InvalidKeyError, AssertionError):
        return HttpResponse(escape(_(u'Invalid location.')))

    course = get_course_with_access(request.user, 'load', course_key)
    staff_access = has_access(request.user, 'staff', course)

    # Permission Denied if they don't have staff access and are trying to see
    # somebody else's submission history.
    if (student_username != request.user.username) and (not staff_access):
        raise PermissionDenied

    user_state_client = DjangoXBlockUserStateClient()
    try:
        history_entries = user_state_client.get_history(student_username, usage_key)
    except DjangoXBlockUserStateClient.DoesNotExist:
        return HttpResponse(escape(_(u'User {username} has never accessed problem {location}').format(
            username=student_username,
            location=location
        )))

    context = {
        'history_entries': history_entries,
        'username': student_username,
        'location': location,
        'course_id': course_key.to_deprecated_string()
    }

    return render_to_response('courseware/submission_history.html', context)


def notification_image_for_tab(course_tab, user, course):
    """
    Returns the notification image path for the given course_tab if applicable, otherwise None.
    """

    tab_notification_handlers = {
        StaffGradingTab.type: open_ended_notifications.staff_grading_notifications,
        PeerGradingTab.type: open_ended_notifications.peer_grading_notifications,
        OpenEndedGradingTab.type: open_ended_notifications.combined_notifications
    }

    if course_tab.name in tab_notification_handlers:
        notifications = tab_notification_handlers[course_tab.name](course, user)
        if notifications and notifications['pending_grading']:
            return notifications['img_path']

    return None


def get_static_tab_contents(request, course, tab):
    """
    Returns the contents for the given static tab
    """
    loc = course.id.make_usage_key(
        tab.type,
        tab.url_slug,
    )
    field_data_cache = FieldDataCache.cache_for_descriptor_descendents(
        course.id, request.user, modulestore().get_item(loc), depth=0
    )
    tab_module = get_module(
        request.user, request, loc, field_data_cache, static_asset_path=course.static_asset_path, course=course
    )

    logging.debug('course_module = {0}'.format(tab_module))

    html = ''
    if tab_module is not None:
        try:
            html = tab_module.render(STUDENT_VIEW).content
        except Exception:  # pylint: disable=broad-except
            html = render_to_string('courseware/error-message.html', None)
            log.exception(
                u"Error rendering course={course}, tab={tab_url}".format(course=course, tab_url=tab['url_slug'])
            )

    return html


@require_GET
@ensure_valid_course_key
def get_course_lti_endpoints(request, course_id):
    """
    View that, given a course_id, returns the a JSON object that enumerates all of the LTI endpoints for that course.

    The LTI 2.0 result service spec at
    http://www.imsglobal.org/lti/ltiv2p0/uml/purl.imsglobal.org/vocab/lis/v2/outcomes/Result/service.html
    says "This specification document does not prescribe a method for discovering the endpoint URLs."  This view
    function implements one way of discovering these endpoints, returning a JSON array when accessed.

    Arguments:
        request (django request object):  the HTTP request object that triggered this view function
        course_id (unicode):  id associated with the course

    Returns:
        (django response object):  HTTP response.  404 if course is not found, otherwise 200 with JSON body.
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)

    try:
        course = get_course(course_key, depth=2)
    except ValueError:
        return HttpResponse(status=404)

    anonymous_user = AnonymousUser()
    anonymous_user.known = False  # make these "noauth" requests like module_render.handle_xblock_callback_noauth
    lti_descriptors = modulestore().get_items(course.id, qualifiers={'category': 'lti'})

    lti_noauth_modules = [
        get_module_for_descriptor(
            anonymous_user,
            request,
            descriptor,
            FieldDataCache.cache_for_descriptor_descendents(
                course_key,
                anonymous_user,
                descriptor
            ),
            course_key,
            course=course
        )
        for descriptor in lti_descriptors
    ]

    endpoints = [
        {
            'display_name': module.display_name,
            'lti_2_0_result_service_json_endpoint': module.get_outcome_service_url(
                service_name='lti_2_0_result_rest_handler') + "/user/{anon_user_id}",
            'lti_1_1_result_service_xml_endpoint': module.get_outcome_service_url(
                service_name='grade_handler'),
        }
        for module in lti_noauth_modules
    ]

    return HttpResponse(json.dumps(endpoints), content_type='application/json')


@login_required
def course_survey(request, course_id):
    """
    URL endpoint to present a survey that is associated with a course_id
    Note that the actual implementation of course survey is handled in the
    views.py file in the Survey Djangoapp
    """

    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)
    course = get_course_with_access(request.user, 'load', course_key)

    redirect_url = reverse('info', args=[course_id])

    # if there is no Survey associated with this course,
    # then redirect to the course instead
    if not course.course_survey_name:
        return redirect(redirect_url)

    return survey.views.view_student_survey(
        request.user,
        course.course_survey_name,
        course=course,
        redirect_url=redirect_url,
        is_required=course.course_survey_required,
    )


def is_course_passed(course, grade_summary=None, student=None, request=None):
    """
    check user's course passing status. return True if passed

    Arguments:
        course : course object
        grade_summary (dict) : contains student grade details.
        student : user object
        request (HttpRequest)

    Returns:
        returns bool value
    """
    nonzero_cutoffs = [cutoff for cutoff in course.grade_cutoffs.values() if cutoff > 0]
    success_cutoff = min(nonzero_cutoffs) if nonzero_cutoffs else None

    if grade_summary is None:
        grade_summary = grades.grade(student, request, course)

    return success_cutoff and grade_summary['percent'] >= success_cutoff


@require_POST
def generate_user_cert(request, course_id):
    """Start generating a new certificate for the user.

    Certificate generation is allowed if:
    * The user has passed the course, and
    * The user does not already have a pending/completed certificate.

    Note that if an error occurs during certificate generation
    (for example, if the queue is down), then we simply mark the
    certificate generation task status as "error" and re-run
    the task with a management command.  To students, the certificate
    will appear to be "generating" until it is re-run.

    Args:
        request (HttpRequest): The POST request to this view.
        course_id (unicode): The identifier for the course.

    Returns:
        HttpResponse: 200 on success, 400 if a new certificate cannot be generated.

    """

    if not request.user.is_authenticated():
        log.info(u"Anon user trying to generate certificate for %s", course_id)
        return HttpResponseBadRequest(
            _('You must be signed in to {platform_name} to create a certificate.').format(
                platform_name=settings.PLATFORM_NAME
            )
        )

    student = request.user
    course_key = CourseKey.from_string(course_id)

    course = modulestore().get_course(course_key, depth=2)
    if not course:
        return HttpResponseBadRequest(_("Course is not valid"))

    if not is_course_passed(course, None, student, request):
        return HttpResponseBadRequest(_("Your certificate will be available when you pass the course."))

    certificate_status = certs_api.certificate_downloadable_status(student, course.id)

    if certificate_status["is_downloadable"]:
        return HttpResponseBadRequest(_("Certificate has already been created."))
    elif certificate_status["is_generating"]:
        return HttpResponseBadRequest(_("Certificate is being created."))
    else:
        # If the certificate is not already in-process or completed,
        # then create a new certificate generation task.
        # If the certificate cannot be added to the queue, this will
        # mark the certificate with "error" status, so it can be re-run
        # with a management command.  From the user's perspective,
        # it will appear that the certificate task was submitted successfully.
        certs_api.generate_user_certificates(student, course.id, course=course, generation_mode='self')
        _track_successful_certificate_generation(student.id, course.id)
        return HttpResponse()


def _track_successful_certificate_generation(user_id, course_id):  # pylint: disable=invalid-name
    """
    Track a successful certificate generation event.

    Arguments:
        user_id (str): The ID of the user generting the certificate.
        course_id (CourseKey): Identifier for the course.
    Returns:
        None

    """
    if settings.FEATURES.get('SEGMENT_IO_LMS') and hasattr(settings, 'SEGMENT_IO_LMS_KEY'):
        event_name = 'edx.bi.user.certificate.generate'  # pylint: disable=no-member
        tracking_context = tracker.get_tracker().resolve_context()  # pylint: disable=no-member

        analytics.track(
            user_id,
            event_name,
            {
                'category': 'certificates',
                'label': unicode(course_id)
            },
            context={
                'Google Analytics': {
                    'clientId': tracking_context.get('client_id')
                }
            }
        )


@require_http_methods(["GET", "POST"])
def render_xblock(request, usage_key_string, check_if_enrolled=True):
    """
    Returns an HttpResponse with HTML content for the xBlock with the given usage_key.
    The returned HTML is a chromeless rendering of the xBlock (excluding content of the containing courseware).
    """
    usage_key = UsageKey.from_string(usage_key_string)
    usage_key = usage_key.replace(course_key=modulestore().fill_in_run(usage_key.course_key))
    course_key = usage_key.course_key

    with modulestore().bulk_operations(course_key):
        # verify the user has access to the course, including enrollment check
        course = get_course_with_access(request.user, 'load', course_key, check_if_enrolled=check_if_enrolled)

        # get the block, which verifies whether the user has access to the block.
        block, _ = get_module_by_usage_id(
            request, unicode(course_key), unicode(usage_key), disable_staff_debug_info=True, course=course
        )

        context = {
            'fragment': block.render('student_view', context=request.GET),
            'course': course,
            'disable_accordion': True,
            'allow_iframing': True,
            'disable_header': True,
            'disable_window_wrap': True,
            'disable_preview_menu': True,
            'staff_access': has_access(request.user, 'staff', course),
            'xqa_server': settings.FEATURES.get('XQA_SERVER', 'http://your_xqa_server.com'),
        }
        return render_to_response('courseware/courseware-chromeless.html', context)
