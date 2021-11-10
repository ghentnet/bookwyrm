""" testing import """
from collections import namedtuple
import csv
import pathlib
from unittest.mock import patch
import datetime
import pytz

from django.test import TestCase
import responses

from bookwyrm import models
from bookwyrm.importers import Importer
from bookwyrm.importers.importer import import_data, handle_imported_book


def make_date(*args):
    """helper function to easily generate a date obj"""
    return datetime.datetime(*args, tzinfo=pytz.UTC)


# pylint: disable=consider-using-with
@patch("bookwyrm.suggested_users.rerank_suggestions_task.delay")
@patch("bookwyrm.activitystreams.populate_stream_task.delay")
@patch("bookwyrm.activitystreams.add_book_statuses_task.delay")
class GenericImporter(TestCase):
    """importing from csv"""

    def setUp(self):
        """use a test csv"""

        class TestImporter(Importer):
            """basic importer"""

            mandatory_fields = ["title", "author"]

            def parse_fields(self, entry):
                return {
                    "id": entry["id"],
                    "Title": entry["title"],
                    "Author": entry["author"],
                    "ISBN13": entry["ISBN"],
                    "Star Rating": entry["rating"],
                    "My Rating": entry["rating"],
                    "My Review": entry["review"],
                    "Exclusive Shelf": entry["shelf"],
                    "Date Added": entry["added"],
                    "Date Read": None,
                }

        self.importer = TestImporter()
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        self.csv = open(datafile, "r", encoding=self.importer.encoding)
        with patch("bookwyrm.suggested_users.rerank_suggestions_task.delay"), patch(
            "bookwyrm.activitystreams.populate_stream_task.delay"
        ):
            self.local_user = models.User.objects.create_user(
                "mouse", "mouse@mouse.mouse", "password", local=True
            )

        work = models.Work.objects.create(title="Test Work")
        self.book = models.Edition.objects.create(
            title="Example Edition",
            remote_id="https://example.com/book/1",
            parent_work=work,
        )

    def test_create_job(self, *_):
        """creates the import job entry and checks csv"""
        import_job = self.importer.create_job(
            self.local_user, self.csv, False, "public"
        )
        self.assertEqual(import_job.user, self.local_user)
        self.assertEqual(import_job.include_reviews, False)
        self.assertEqual(import_job.privacy, "public")

        import_items = models.ImportItem.objects.filter(job=import_job).all()
        self.assertEqual(len(import_items), 4)
        self.assertEqual(import_items[0].index, 0)
        self.assertEqual(import_items[0].data["id"], "38")
        self.assertEqual(import_items[1].index, 1)
        self.assertEqual(import_items[1].data["id"], "48")
        self.assertEqual(import_items[2].index, 2)
        self.assertEqual(import_items[2].data["id"], "23")
        self.assertEqual(import_items[3].index, 3)
        self.assertEqual(import_items[3].data["id"], "10")

    def test_create_retry_job(self, *_):
        """trying again with items that didn't import"""
        import_job = self.importer.create_job(
            self.local_user, self.csv, False, "unlisted"
        )
        import_items = models.ImportItem.objects.filter(job=import_job).all()[:2]

        retry = self.importer.create_retry_job(
            self.local_user, import_job, import_items
        )
        self.assertNotEqual(import_job, retry)
        self.assertEqual(retry.user, self.local_user)
        self.assertEqual(retry.include_reviews, False)
        self.assertEqual(retry.privacy, "unlisted")

        retry_items = models.ImportItem.objects.filter(job=retry).all()
        self.assertEqual(len(retry_items), 2)
        self.assertEqual(retry_items[0].index, 0)
        self.assertEqual(retry_items[0].data["id"], "38")
        self.assertEqual(retry_items[1].index, 1)
        self.assertEqual(retry_items[1].data["id"], "48")

    def test_start_import(self, *_):
        """check that a task was created"""
        import_job = self.importer.create_job(
            self.local_user, self.csv, False, "unlisted"
        )
        MockTask = namedtuple("Task", ("id"))
        mock_task = MockTask(7)
        with patch("bookwyrm.importers.importer.import_data.delay") as start:
            start.return_value = mock_task
            self.importer.start_import(import_job)
        import_job.refresh_from_db()
        self.assertEqual(import_job.task_id, "7")

    @responses.activate
    def test_import_data(self, *_):
        """resolve entry"""
        import_job = self.importer.create_job(
            self.local_user, self.csv, False, "unlisted"
        )
        book = models.Edition.objects.create(title="Test Book")

        with patch(
            "bookwyrm.models.import_job.ImportItem.get_book_from_isbn"
        ) as resolve:
            resolve.return_value = book
            with patch("bookwyrm.importers.importer.handle_imported_book"):
                import_data(self.importer.service, import_job.id)

        import_item = models.ImportItem.objects.get(job=import_job, index=0)
        self.assertEqual(import_item.book.id, book.id)

    def test_handle_imported_book(self, *_):
        """import added a book, this adds related connections"""
        shelf = self.local_user.shelf_set.filter(identifier="read").first()
        self.assertIsNone(shelf.books.first())

        import_job = models.ImportJob.objects.create(user=self.local_user)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        csv_file = open(datafile, "r")  # pylint: disable=unspecified-encoding
        for index, entry in enumerate(list(csv.DictReader(csv_file))):
            entry = self.importer.parse_fields(entry)
            import_item = models.ImportItem.objects.create(
                job_id=import_job.id, index=index, data=entry, book=self.book
            )
            break

        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            handle_imported_book(
                self.importer.service, self.local_user, import_item, False, "public"
            )

        shelf.refresh_from_db()
        self.assertEqual(shelf.books.first(), self.book)

    def test_handle_imported_book_already_shelved(self, *_):
        """import added a book, this adds related connections"""
        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            shelf = self.local_user.shelf_set.filter(identifier="to-read").first()
            models.ShelfBook.objects.create(
                shelf=shelf,
                user=self.local_user,
                book=self.book,
                shelved_date=make_date(2020, 2, 2),
            )

        import_job = models.ImportJob.objects.create(user=self.local_user)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        csv_file = open(datafile, "r")  # pylint: disable=unspecified-encoding
        for index, entry in enumerate(list(csv.DictReader(csv_file))):
            entry = self.importer.parse_fields(entry)
            import_item = models.ImportItem.objects.create(
                job_id=import_job.id, index=index, data=entry, book=self.book
            )
            break

        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            handle_imported_book(
                self.importer.service, self.local_user, import_item, False, "public"
            )

        shelf.refresh_from_db()
        self.assertEqual(shelf.books.first(), self.book)
        self.assertEqual(
            shelf.shelfbook_set.first().shelved_date, make_date(2020, 2, 2)
        )
        self.assertIsNone(
            self.local_user.shelf_set.get(identifier="read").books.first()
        )

    def test_handle_import_twice(self, *_):
        """re-importing books"""
        shelf = self.local_user.shelf_set.filter(identifier="read").first()
        import_job = models.ImportJob.objects.create(user=self.local_user)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        csv_file = open(datafile, "r")  # pylint: disable=unspecified-encoding
        for index, entry in enumerate(list(csv.DictReader(csv_file))):
            entry = self.importer.parse_fields(entry)
            import_item = models.ImportItem.objects.create(
                job_id=import_job.id, index=index, data=entry, book=self.book
            )
            break

        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            handle_imported_book(
                self.importer.service, self.local_user, import_item, False, "public"
            )
            handle_imported_book(
                self.importer.service, self.local_user, import_item, False, "public"
            )

        shelf.refresh_from_db()
        self.assertEqual(shelf.books.first(), self.book)

    @patch("bookwyrm.activitystreams.add_status_task.delay")
    def test_handle_imported_book_review(self, *_):
        """review import"""
        import_job = models.ImportJob.objects.create(user=self.local_user)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        csv_file = open(datafile, "r")  # pylint: disable=unspecified-encoding
        entry = list(csv.DictReader(csv_file))[3]
        entry = self.importer.parse_fields(entry)
        import_item = models.ImportItem.objects.create(
            job_id=import_job.id, index=0, data=entry, book=self.book
        )

        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            with patch("bookwyrm.models.Status.broadcast") as broadcast_mock:
                handle_imported_book(
                    self.importer.service,
                    self.local_user,
                    import_item,
                    True,
                    "unlisted",
                )
        kwargs = broadcast_mock.call_args.kwargs
        self.assertEqual(kwargs["software"], "bookwyrm")
        review = models.Review.objects.get(book=self.book, user=self.local_user)
        self.assertEqual(review.content, "mixed feelings")
        self.assertEqual(review.rating, 2.0)
        self.assertEqual(review.privacy, "unlisted")

    @patch("bookwyrm.activitystreams.add_status_task.delay")
    def test_handle_imported_book_rating(self, *_):
        """rating import"""
        import_job = models.ImportJob.objects.create(user=self.local_user)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        csv_file = open(datafile, "r")  # pylint: disable=unspecified-encoding
        entry = list(csv.DictReader(csv_file))[1]
        entry = self.importer.parse_fields(entry)
        import_item = models.ImportItem.objects.create(
            job_id=import_job.id, index=0, data=entry, book=self.book
        )

        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            handle_imported_book(
                self.importer.service, self.local_user, import_item, True, "unlisted"
            )
        review = models.ReviewRating.objects.get(book=self.book, user=self.local_user)
        self.assertIsInstance(review, models.ReviewRating)
        self.assertEqual(review.rating, 3.0)
        self.assertEqual(review.privacy, "unlisted")

    def test_handle_imported_book_reviews_disabled(self, *_):
        """review import"""
        import_job = models.ImportJob.objects.create(user=self.local_user)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/generic.csv")
        csv_file = open(datafile, "r")  # pylint: disable=unspecified-encoding
        entry = list(csv.DictReader(csv_file))[2]
        entry = self.importer.parse_fields(entry)
        import_item = models.ImportItem.objects.create(
            job_id=import_job.id, index=0, data=entry, book=self.book
        )

        with patch("bookwyrm.models.activitypub_mixin.broadcast_task.delay"):
            handle_imported_book(
                self.importer.service, self.local_user, import_item, False, "unlisted"
            )
        self.assertFalse(
            models.Review.objects.filter(book=self.book, user=self.local_user).exists()
        )
