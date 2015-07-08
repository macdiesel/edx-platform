"""
Unittests for deleting a course in an chosen modulestore
"""

import unittest
import ddt
from django.core.management import CommandError, call_command
from contentstore.management.commands.delete_course import Command
from contentstore.management.commands.create_course import Command as CreateCourse
from contentstore.tests.utils import CourseTestCase
from xmodule.modulestore.tests.factories import CourseFactory
from xmodule.modulestore.django import modulestore


class TestArgParsing(unittest.TestCase):
    """
    Tests for parsing arguments for the 'delete_course' management command
    """

    def setUp(self):
        super(TestArgParsing, self).setUp()

        self.command = Command()

    def test_no_args(self):
        errstring = "Arguments missing: 'org/number/run commit'"
        with self.assertRaisesRegexp(CommandError, errstring):
            self.command.handle()

    def test_no_course_key(self):
        errstring = "Delete_course requires a course_id <org/number/run> argument."
        with self.assertRaisesRegexp(CommandError, errstring):
            self.command.handle("commit")

    def test_commit_argument(self):
        errstring = "Delete_course requires a commit argument at the end"
        with self.assertRaisesRegexp(CommandError, errstring):
            self.command.handle("TestX/TS01/run")

    def test_invalid_course_key(self):
        errstring = "Invalid course_key: 'TestX/TS01'. Proper syntax: 'org/number/run commit' "
        with self.assertRaisesRegexp(CommandError, errstring):
            self.command.handle("TestX/TS01", "commit")

    def test_missing_commit_argument(self):
        errstring = "Delete_course requires a commit argument at the end"
        with self.assertRaisesRegexp(CommandError, errstring):
            self.command.handle("TestX/TS01/run", "comit")
    

class DeleteCourseTest(CourseTestCase):

    def setUp(self):
        super(DeleteCourseTest, self).setUp()

        self.command = Command()

        org = 'TestX'
        course_number = 'TS01'
        course_run = '2015_Q1'

        # Create a course using split modulestore
        self.course = CourseFactory.create(
            org=org,
            number=course_number,
            run=course_run
        )

    def test_courses_keys_listing(self):
        courses = [str(key) for key in modulestore().get_courses_keys()]
        self.assertIn("TestX/TS01/2015_Q1", courses)

    def test_course_deleted(self):
        self.command.handle("TestX/TS01/2015_Q1", "commit")
        courses = [str(key) for key in modulestore().get_courses_keys()]
        self.assertNotIn("TestX/TS01/2015_Q1", courses)
