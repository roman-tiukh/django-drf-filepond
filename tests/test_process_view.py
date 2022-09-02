from django_drf_filepond.models import TemporaryUploadChunked
from io import BytesIO
import logging
import os
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.client import encode_multipart, RequestFactory
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from six import ensure_text

from django_drf_filepond import drf_filepond_settings, uploaders
import django_drf_filepond
import django_drf_filepond.views as views
from tests.utils import remove_file_upload_dir_if_required


# Python 2/3 support
try:
    from unittest.mock import patch, MagicMock, Mock, ANY
except ImportError:
    from mock import patch, MagicMock, Mock, ANY

LOG = logging.getLogger(__name__)


#
# New tests for checking file storage outside of BASE_DIR (see #18)
#
# test_store_upload_with_storage_outside_BASE_DIR_without_enable: Set a
#    FileSystemStorage store location that is outside of BASE_DIR and check
#    that the upload fails.
#
# test_store_upload_with_storage_outside_BASE_DIR_with_enable: Set a
#    FileSystemStorage store location outside of BASE_DIR and also set the
#    ALLOW_EXTERNAL_UPLOAD_DIR setting to True and check the upload succeeds
#
# test_relative_UPLOAD_TMP_outside_base_dir_not_allowed: Check that when a
#    a relative path is provided that gets around the requirement for
#    UPLOAD_TMP to be under BASE_DIR, that this is detected and a 500 thrown
#
# test_new_chunked_upload_request: Check that a new chunked upload request
#    results in handle_upload being called on the chunked uploader class.
#
# test_chunked_upload_large_file: Test that a new chunked upload request for
#    a file greater than ~2GB in size is correctly handled.
#
# UPDATE: June 2021:
# test_process_data_BASE_DIR_pathlib: Tests the upload process when BASE_DIR
#    is set as a pathlib.Path object as it is by default in more recent Django
#    versions - at present, django-drf-filepond uses regular strings for paths
class ProcessTestCase(TestCase):

    def setUp(self):
        # Create some random data to test upload.
        data = BytesIO()
        data.write(os.urandom(16384))
        self.test_data = data

        self.rf = RequestFactory()

    def test_process_data(self):
        self._process_data()

    def test_process_data_BASE_DIR_pathlib(self):
        # In recent Django versions, BASE_DIR is set by default to
        # Path(__file__).resolve().parent.parent when creating a new project
        # using django-admin. Older versions of Django, in use when
        # django-drf-filepond was originally created used regular strings
        # for paths. Need to be able to handle both.
        # Set modified BASE_DIR using context manager - on older Python
        # versions that don't have pathlib support, fall back to strings
        OLD_BASE_DIR = drf_filepond_settings.BASE_DIR
        try:
            from pathlib import Path
            NEW_BASE_DIR = Path(__file__).resolve().parent.parent
            LOG.debug('PATHLIB TEST: Old BASE_DIR: %s   NEW_BASE_DIR: %s' %
                      (repr(drf_filepond_settings.BASE_DIR),
                       repr(NEW_BASE_DIR)))
            drf_filepond_settings.BASE_DIR = NEW_BASE_DIR
        except ImportError:
            LOG.debug('NO PATHLIB SUPPORT FOR PATHLIB TEST. '
                      'FALLING BACK TO USING REGULAR STRING PATHS...')

        try:
            self._process_data()
        finally:
            drf_filepond_settings.BASE_DIR = OLD_BASE_DIR

    def test_UPLOAD_TMP_not_set(self):
        upload_tmp = drf_filepond_settings.UPLOAD_TMP
        delattr(drf_filepond_settings, 'UPLOAD_TMP')

        # Set up and run request
        (encoded_form, content_type) = self._get_encoded_form('testfile.dat')

        req = self.rf.post(reverse('process'),
                      data=encoded_form, content_type=content_type)
        pv = views.ProcessView.as_view()
        response = pv(req)

        self.assertContains(response, 'The file upload path settings are '
                            'not configured correctly.', status_code=500)

        setattr(drf_filepond_settings, 'UPLOAD_TMP', upload_tmp)

    def test_process_invalid_storage_location(self):
        old_storage = views.chunked_storage
        views.chunked_storage = FileSystemStorage(location='/django_test')
        (encoded_form, content_type) = self._get_encoded_form('testfile.dat')

        req = self.rf.post(reverse('process'),
                      data=encoded_form, content_type=content_type)
        pv = views.ProcessView.as_view()
        response = pv(req)
        views.chunked_storage = old_storage
        self.assertEqual(response.status_code, 500, 'Expecting 500 error due'
                         ' to invalid storage location.')
        self.assertEqual(
            response.data,
            'The file upload path settings are not configured correctly.',
            ('Expecting error showing path settings are configured '
             'incorrectly.'))

    def test_process_invalid_data(self):
        upload_form = {'somekey':
                       SimpleUploadedFile('test.txt', self.test_data.read())}
        enc_form = encode_multipart('abc', upload_form)
        req = self.rf.post(reverse('process'), data=enc_form,
                      content_type='multipart/form-data; boundary=abc')
        pv = views.ProcessView.as_view()
        response = pv(req)
        self.assertEqual(response.status_code, 400, 'Expecting 400 error due'
                         ' to invalid data being provided.')
        self.assertTrue('detail' in response.data,
                        'Error detail missing in response.')
        self.assertIn(response.data['detail'], ('Could not find upload_field_name'
                                                ' in request data.'))

    def test_upload_non_file_data(self):
        cf = ContentFile(self.test_data.read(), name='test.txt')
        upload_form = {'filepond': cf}
        enc_form = encode_multipart('abc', upload_form)
        req = self.rf.post(reverse('process'), data=enc_form,
                      content_type='multipart/form-data; boundary=abc')
        req.FILES['filepond'] = cf
        pv = views.ProcessView.as_view()
        response = pv(req)
        self.assertEqual(response.status_code, 400, 'Expecting 400 error due'
                         ' to non-file data being provided.')
        self.assertTrue('detail' in response.data,
                        'Error detail missing in response.')
        self.assertIn(response.data['detail'], ('Invalid data type has been '
                                                'parsed.'))

    # Based on the modification for issue #4, test that we can successfully
    # handle an upload that provides its data under a field name that is not
    # the default "filepond" and that an error is thrown when an invalid
    # name is expected
    def test_process_data_different_field_name(self):
        self._process_data('somefield')

    def test_process_data_invalid_different_field_name(self):
        upload_form = {'somekey': SimpleUploadedFile(
            'test.txt', self.test_data.read()), 'fp_upload_field': 'somekey2'
        }
        boundary = str(uuid.uuid4()).replace('-', '')
        enc_form = encode_multipart(boundary, upload_form)

        req = self.rf.post(reverse('process'), data=enc_form,
                      content_type='multipart/form-data; boundary=%s'
                      % boundary)
        pv = views.ProcessView.as_view()
        response = pv(req)
        self.assertEqual(response.status_code, 400, 'Expecting 400 error due'
                         ' to invalid data being provided.')
        self.assertTrue('detail' in response.data,
                        'Error detail missing in response.')
        self.assertIn(response.data['detail'], ('Could not find upload_field_name '
                                                'in request data.'))

    def test_store_upload_with_storage_outside_BASE_DIR_without_enable(self):
        old_storage = views.chunked_storage
        views.chunked_storage = FileSystemStorage(location='/tmp/uploads')
        (encoded_form, content_type) = self._get_encoded_form('testfile.dat')

        req = self.rf.post(reverse('process'),
                      data=encoded_form, content_type=content_type)
        pv = views.ProcessView.as_view()
        response = pv(req)
        views.chunked_storage = old_storage
        self.assertEqual(response.status_code, 500, 'Expecting 500 error due'
                         ' to invalid storage location.')
        self.assertEqual(
            response.data,
            'The file upload path settings are not configured correctly.',
            ('Expecting error showing path settings are configured '
             'incorrectly.'))

    def test_store_upload_with_storage_outside_BASE_DIR_with_enable(self):
        old_storage = views.chunked_storage
        old_UPLOAD_TMP = drf_filepond_settings.UPLOAD_TMP

        drf_filepond_settings.ALLOW_EXTERNAL_UPLOAD_DIR = True

        views.chunked_storage = FileSystemStorage(location='/tmp/uploads')
        drf_filepond_settings.UPLOAD_TMP = '/tmp/uploads'

        (encoded_form, content_type) = self._get_encoded_form('testfile.dat')

        req = self.rf.post(reverse('process'),
                      data=encoded_form, content_type=content_type)
        pv = views.ProcessView.as_view()
        response = pv(req)
        views.chunked_storage = old_storage
        drf_filepond_settings.UPLOAD_TMP = old_UPLOAD_TMP
        drf_filepond_settings.ALLOW_EXTERNAL_UPLOAD_DIR = False
        self.assertEqual(response.status_code, 200, 'Expecting upload to be '
                         'successful.')

    def test_relative_UPLOAD_TMP_outside_base_dir_not_allowed(self):
        upload_tmp = drf_filepond_settings.UPLOAD_TMP
        drf_filepond_settings.UPLOAD_TMP = os.path.join(
            drf_filepond_settings.BASE_DIR, '..', '..', 'some_dir')

        # Set up and run request
        (encoded_form, content_type) = self._get_encoded_form('testfile.dat')

        req = self.rf.post(reverse('process'),
                      data=encoded_form, content_type=content_type)
        pv = views.ProcessView.as_view()
        response = pv(req)

        self.assertContains(response, 'An invalid storage location has been '
                            'specified.', status_code=500)

        drf_filepond_settings.UPLOAD_TMP = upload_tmp

    @patch('django_drf_filepond.uploaders.FilepondChunkedFileUploader.'
           '_handle_new_chunk_upload')
    def test_new_chunked_upload_request(self, mock_chunked_ul):
        # Tried to patch the mocked _get_file_id function but the views
        # module seems to have been pre-initialised within the Django init
        # phase and it has already imported the unmocked object.
        # For testing we manually assign the mocked object here and then
        # revert to the original after the view call.
        upload_id = ensure_text('ababababababababababab')
        file_id = ensure_text('xyxyxyxyxyxyxyxyxyxyxy')
        mock_gfid = MagicMock(spec='django_drf_filepond.utils._get_file_id')
        mock_gfid.side_effect = [upload_id, file_id, upload_id, file_id]

        original_gfid = django_drf_filepond.views._get_file_id
        django_drf_filepond.views._get_file_id = mock_gfid

        mock_chunked_ul.return_value = Response(upload_id,
                                                status=status.HTTP_200_OK,
                                                content_type='text/plain')
        (encoded_form, content_type) = self._get_encoded_form('testfile.dat',
                                                              file_spec='{}')
        req = self.rf.post(reverse('process'),
                           data=encoded_form, content_type=content_type,
                           HTTP_UPLOAD_LENGTH=1048576)
        pv = views.ProcessView.as_view()
        pv(req)
        django_drf_filepond.views._get_file_id = original_gfid

        mock_chunked_ul.assert_called_once_with(ANY, upload_id, file_id)

    def test_chunked_upload_large_file(self):
        # Mock _get_file_id to return the specified file and upload IDs.
        upload_id = ensure_text('ababababababababababab')
        file_id = ensure_text('xyxyxyxyxyxyxyxyxyxyxy')
        mock_gfid = MagicMock(spec='django_drf_filepond.utils._get_file_id')
        mock_gfid.side_effect = [upload_id, file_id, upload_id, file_id]
        original_gfid = django_drf_filepond.views._get_file_id
        django_drf_filepond.views._get_file_id = mock_gfid

        (encoded_form, content_type) = self._get_encoded_form('largefile.dat',
                                                              file_spec='{}')
        # Create a post request for a 3GB file...
        req = self.rf.post(reverse('process'),
                           data=encoded_form, content_type=content_type,
                           HTTP_UPLOAD_LENGTH=3221225472)
        # Run the request and check for the TemporaryUploadChunked object
        pv = views.ProcessView.as_view()
        # mock the os.makedirs and os.path.exists functions here so that no
        # dir is created during the creation of the chunked uploader object.
        os = MagicMock()
        os.path = MagicMock()
        os.path.exists = Mock(return_value=True)
        os.path.abspath = Mock(return_value=drf_filepond_settings.UPLOAD_TMP)
        os.makedirs = MagicMock()
        with patch('django_drf_filepond.uploaders.os', os):
            response = pv(req)
        django_drf_filepond.views._get_file_id = original_gfid
        self.assertEqual(response.status_code, 200)
        tuc = TemporaryUploadChunked.objects.get(upload_id=upload_id)
        self.assertEqual(tuc.total_size, 3221225472)

    def _process_data(self, upload_field_name=None):
        tmp_upload_dir = drf_filepond_settings.UPLOAD_TMP
        self.uploaddir_exists_pre_test = os.path.exists(tmp_upload_dir)
        (encoded_form, content_type) = self._get_encoded_form(
            'testfile.dat', upload_field_name)

        response = self.client.post(
            reverse('process'),
            data=encoded_form, content_type=content_type)

        # Attempt created file/directory removal before assert statements
        # so that we can clean up as far as possible at this stage.
        if hasattr(response, 'data'):
            dir_path = os.path.join(tmp_upload_dir, response.data)
            if len(response.data) == 22 and os.path.exists(dir_path):
                dir_list = os.listdir(dir_path)
                if len(dir_list) == 1 and len(dir_list[0]) == 22:
                    file_path = os.path.join(dir_path, dir_list[0])
                    LOG.debug('Removing generated file <%s>' % file_path)
                    os.remove(file_path)
                    LOG.debug('Removing temporary directory <%s>'
                              % dir_path)
                    os.rmdir(dir_path)
                else:
                    LOG.warning('Name of uploaded file in the temp '
                                'directory doesn\'t have 22 chars, '
                                'not deleting the file')
            else:
                LOG.error('Couldn\'t proceed with file deleting since the '
                          'response received was not the right length (22)')

        remove_file_upload_dir_if_required(self.uploaddir_exists_pre_test,
                                           tmp_upload_dir)

        self.assertEqual(response.status_code, 200,
                         'Response received status code <%s> instead of 200.'
                         % (response.status_code))

        self.assertTrue(hasattr(response, 'data'),
                        ('The response does not contain a data attribute.'))

        self.assertEqual(len(response.data), 22,
                         'Response data is not of the correct length.')

    def _get_encoded_form(self, filename, upload_field_name=None,
                          file_spec=None):
        if not file_spec:
            file_spec = SimpleUploadedFile(filename, self.test_data.read())
        if upload_field_name:
            upload_form = {upload_field_name: file_spec,
                           'fp_upload_field': upload_field_name}
        else:
            upload_form = {'filepond': file_spec}

        boundary = str(uuid.uuid4()).replace('-', '')

        encoded_form = encode_multipart(boundary, upload_form)

        content_type = ('multipart/form-data; boundary=%s' % (boundary))

        return (encoded_form, content_type)
