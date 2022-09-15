# This module contains API functions exported through the top-level
# django_drf_filepond module.
#
# store_upload: used to move an upload from temporary storage to a permanent
#               storage location. If you're using local file storage, this
#               requires that you have the DJANGO_DRF_FILEPOND_FILE_STORE_PATH
#               setting set in your application's settings.py file.
#
import logging
import ntpath
import os
import shutil

import django_drf_filepond.drf_filepond_settings as local_settings
from django.core.exceptions import ImproperlyConfigured
import re
import shortuuid
from django_drf_filepond.models import TemporaryUpload, StoredUpload
from django_drf_filepond.storage_utils import _get_storage_backend
from django_drf_filepond.exceptions import ConfigurationError
from django_drf_filepond.utils import is_image_for_thumbnail
from sorl.thumbnail import get_thumbnail

# TODO: Need to refactor this into a class and put the initialisation of
# the storage backend into the init.
storage_backend_initialised = False
storage_backend = None

LOG = logging.getLogger(__name__)

# There's no built in FileNotFoundError, FileExistsError in Python 2
try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError

try:
    FileExistsError
except NameError:
    FileExistsError = OSError


def _init_storage_backend():
    global storage_backend_initialised
    global storage_backend

    storage_module_name = getattr(local_settings, 'STORAGES_BACKEND', None)
    LOG.debug('Initialising storage backend with storage module name [%s]'
              % storage_module_name)
    storage_backend = _get_storage_backend(storage_module_name)
    storage_backend_initialised = True


# Store the temporary upload represented by upload_id to the specified
# destination_file_path under the defined file store location as specified by
# the DJANGO_DRF_FILEPOND_FILE_STORE_PATH configuration setting (for local
# storage). Files stored using this approach can subsequently be retrieved
# using the load method defined in by the filepond server spec by using
# either the 22-char upload_id or the value provided to the
# destination_file_path parameter as a query string parameter using the
# "id" key.
def store_upload(upload_id):
    """
    Store the temporary upload with the specified upload ID to the
    destination_file_path. destination_file_path should be a directory only
    and not include the target name of the file.

    If destination_file_name is not provided, the file
    is stored using the name it was originally uploaded with. If
    destination_file_name is provided, this is the name used to store the
    file. i.e. the file will be stored at
        destination_file_path + destination_file_name
    """
    # TODO: If the storage backend is not initialised, init now - this will
    # be removed when this module is refactored into a class.
    if not storage_backend_initialised:
        _init_storage_backend()

    id_fmt = re.compile('^([%s]){22}$' % (shortuuid.get_alphabet()))
    if not id_fmt.match(upload_id):
        LOG.error('The provided upload ID <%s> is of an invalid format.'
                  % upload_id)
        raise ValueError('The provided upload ID is of an invalid format.')

    try:
        temp_upload = TemporaryUpload.objects.get(upload_id=upload_id)
    except TemporaryUpload.DoesNotExist:
        raise ValueError('Record for the specified upload_id doesn\'t exist')

    su = None
    try:
        su = StoredUpload(upload_id=temp_upload.upload_id,
                          file=temp_upload.file.name,
                          uploaded=temp_upload.uploaded,
                          uploaded_by=temp_upload.uploaded_by)
        su.save()
        temp_upload.delete()
    except Exception as e:
        errorMsg = ('Error storing temporary upload to remote storage: [%s]'
                    % str(e))
        LOG.error(errorMsg)
        raise e

    return su


def get_stored_upload(upload_id):
    """
    Get an upload that has previously been stored using the store_upload
    function.

    upload_id: This function takes a 22-character unique ID assigned to the
    original upload of the requested file.
    """
    # If the parameter matched the upload ID format, we assume that it
    # must be an upload ID and proceed accordingly. If the lookup of the
    # record fails, then we have another go assuming a filename was
    # instead provided.

    # NOTE: The API doesn't officially provide support for requesting stored
    # uploads by filename. This is retained here for backward compatibility
    # but it is DEPRECATED and will be removed in a future release.
    param_filename = False

    upload_id_fmt = re.compile('^([%s]){22}$'
                               % (shortuuid.get_alphabet()))

    if not upload_id_fmt.match(upload_id):
        param_filename = True
        LOG.debug('The provided string doesn\'t seem to be an '
                  'upload ID. Assuming it is a filename/path.')

    if not param_filename:
        try:
            su = StoredUpload.objects.get(upload_id=upload_id)
        except StoredUpload.DoesNotExist:
            LOG.debug('A StoredUpload with the provided ID doesn\'t '
                      'exist. Assuming this could be a filename.')
            param_filename = True

    if param_filename:
        # Try and lookup a StoredUpload record with the specified id
        # as the file path
        try:
            su = StoredUpload.objects.get(file=upload_id)
        except StoredUpload.DoesNotExist as e:
            LOG.debug('A StoredUpload with the provided file path '
                      'doesn\'t exist. Re-raising error')
            raise e

    return su


def get_stored_upload_file_data(stored_upload, thumbnail_type):
    """
    Given a StoredUpload object, this function gets and returns the data of
    the file associated with the StoredUpload instance.

    This function provides an abstraction over the storage backend, accessing
    the file data regardless of whether the file is stored on the local
    filesystem or on some remote storage service, e.g. Amazon S3. Supported
    storage backends are those supported by the django-storages library.

    Returns a tuple (filename, data_bytes_io).
        filename is a string containing the name of the stored file
        data_bytes_io is a file-like BytesIO object containing the file data
    """
    # TODO: If the storage backend is not initialised, init now - this
    # will be removed when this module is refactored into a class.
    if not storage_backend_initialised:
        _init_storage_backend()
    if storage_backend:
        LOG.debug('get_stored_upload_file_data: Using a remote storage '
                  'service: [%s]' % (type(storage_backend).__name__))

        file_path_base = ''
    else:
        LOG.debug('get_stored_upload_file_data: Using local storage backend.')
        if ((not hasattr(local_settings, 'FILE_STORE_PATH')) or
                (not local_settings.FILE_STORE_PATH) or
                (not os.path.exists(local_settings.FILE_STORE_PATH)) or
                (not os.path.isdir(local_settings.FILE_STORE_PATH))):
            raise ConfigurationError('The file upload settings are not '
                                     'configured correctly.')

        file_path_base = local_settings.FILE_STORE_PATH
        #  This code is redundant, this case will be picked up by the
        #  not local_settings.FILE_STORE_PATH in the above statement.
        #   if not file_path_base:
        #       file_path_base = ''

    # See if the stored file with the path specified in su exists
    # in the file store location
    file_path = os.path.join(file_path_base, stored_upload.file.name)
    if storage_backend:
        if not storage_backend.exists(file_path):
            LOG.error('File [%s] for upload_id [%s] not found on remote '
                      'file store' % (file_path, stored_upload.upload_id))
            raise FileNotFoundError(
                'File [%s] for upload_id [%s] not found on remote file '
                'store.' % (file_path, stored_upload.upload_id))
    else:
        if ((not os.path.exists(file_path)) or
                (not os.path.isfile(file_path))):
            LOG.error('File [%s] for upload_id [%s] not found on local disk'
                      % (file_path, stored_upload.upload_id))
            raise FileNotFoundError('File [%s] not found on local disk'
                                    % file_path)

        # We now know that the file exists locally and is not a directory

    filename = os.path.basename(stored_upload.file.name)
    if is_image_for_thumbnail(filename) and thumbnail_type and local_settings.THUMBNAIL_SIZES:
        thumbnail_config = local_settings.THUMBNAIL_SIZES.get(thumbnail_type, None)
        if not thumbnail_config:
            LOG.error(f'Unknown thumbnail size type [{thumbnail_type}] - falling back to default. ' +
                'Set thumbnail config via DJANGO_DRF_FILEPOND_THUMBNAIL_SIZES setting key.')
            thumbnail_config = '300x300'
        thumbnailed_solr = get_thumbnail(stored_upload.file, thumbnail_config)
        if not thumbnailed_solr.exists():
            LOG.error(f'Failed to produce a thumbnail [{thumbnail_type}] with config [{thumbnail_config}].')
            # returning empty file so on UI it will appear with download button
            return (filename, stored_upload.file.read())
        return (filename, thumbnailed_solr.read())
    return (filename, stored_upload.file.read())


def delete_stored_upload(upload_id, delete_file=False):
    """
    Delete the specified stored upload AND IF delete_file=True ALSO
    PERMANENTLY DELETE THE FILE ASSOCIATED WITH THE UPLOAD.

    It is necessary to explicitly set delete_file=True to ensure that it
    is made explicit that the stored file associated with the upload will be
    permanently deleted.
    """
    try:
        su = get_stored_upload(upload_id)
    except StoredUpload.DoesNotExist as e:
        LOG.error('No stored upload found with the specified ID [%s].'
                  % (upload_id))
        raise e

    # Need to retain upload ID here since this is used in error messages later
    upload_id = su.upload_id

    su.delete()

    if not delete_file:
        return True

    # If we got the stored file record and delete_file is True, make sure
    # that the storage backend is set up and we have access to it.
    # TODO: If the storage backend is not initialised, init now - this
    # will be removed when this module is refactored into a class.
    if not storage_backend_initialised:
        _init_storage_backend()

    if storage_backend:
        LOG.debug('delete_stored_upload: Using a remote storage '
                  'service: [%s]' % (type(storage_backend).__name__))
        file_path_base = ''
    else:
        LOG.debug('delete_stored_upload: Using local storage backend.')
        if ((not hasattr(local_settings, 'FILE_STORE_PATH')) or
                (not local_settings.FILE_STORE_PATH) or
                (not os.path.exists(local_settings.FILE_STORE_PATH)) or
                (not os.path.isdir(local_settings.FILE_STORE_PATH))):
            raise ConfigurationError('The file upload settings are not '
                                     'configured correctly.')

        file_path_base = local_settings.FILE_STORE_PATH

    file_path = os.path.join(file_path_base, su.file.name)
    if storage_backend:
        if not storage_backend.exists(file_path):
            LOG.error('Stored upload file [%s] with upload_id [%s] is not '
                      'found on remote file store' % (file_path, upload_id))
            raise FileNotFoundError(
                'File [%s] for stored upload with id [%s] not found on remote'
                ' file store.' % (file_path, upload_id))
        storage_backend.delete(file_path)
    # Else delete local file
    else:
        if ((not os.path.exists(file_path)) or
                (not os.path.isfile(file_path))):
            LOG.error('File [%s] for stored upload [%s] not found on '
                      'local disk' % (file_path, upload_id))
            raise FileNotFoundError('File [%s] to delete was not found on '
                                    'the local disk' % file_path)

        # We now know that the file exists locally and is not a directory
        try:
            os.remove(file_path)
        except OSError as e:
            LOG.error('Error removing requested file: %s' % str(e))
            raise e

        # TODO: Need to look at how best to delete directories that may have
        # been created to store the file. For now, we just delete the file.

    return True
