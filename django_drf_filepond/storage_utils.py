import importlib
import logging

import django_drf_filepond.drf_filepond_settings as local_settings
from django.core.exceptions import ImproperlyConfigured
import os

LOG = logging.getLogger(__name__)

storage_backend_initialised = False
storage_backend = None

def _get_storage_backend(fq_classname):
    """
    Load the specified django-storages storage backend class. This is called
    regardless of whether a beckend is specified so if fq_classname is not
    set, we just return None.

    fq_classname is a string specifying the fully-qualified class name of
    the django-storages backend to use, e.g.
        'storages.backends.sftpstorage.SFTPStorage'
    """
    LOG.debug('Running _get_storage_backend with fq_classname [%s]'
              % fq_classname)

    if not fq_classname:
        return None

    (modname, clname) = fq_classname.rsplit('.', 1)
    # A test import of the backend storage class should have been undertaken
    # at app startup in django_drf_filepond.apps.ready so any failure
    # importing the backend should have been picked up then.
    mod = importlib.import_module(modname)
    storage_backend = getattr(mod, clname)()
    LOG.info('Storage backend instance [%s] created...' % fq_classname)

    return storage_backend


def init_storage_backend():
    storage_module_name = getattr(local_settings, 'STORAGES_BACKEND', None)
    LOG.debug('Initialising storage backend with storage module name [%s]'
              % storage_module_name)
    storage_backend = _get_storage_backend(storage_module_name)

    # If there's no storage backend set then we're using local file storage
    # and FILE_STORE_PATH must be set.
    if storage_backend:
        if ((not hasattr(local_settings, 'FILE_STORE_PATH')) or
                (not local_settings.FILE_STORE_PATH)):
            raise ImproperlyConfigured('A required setting is missing in your '
                                       'application configuration.')
        file_path_base = local_settings.FILE_STORE_PATH
        if((not os.path.exists(file_path_base)) or
                (not os.path.isdir(file_path_base))):
            raise FileNotFoundError(
                'The local output directory [%s] defined by FILE_STORE_PATH is '
                'missing.' % file_path_base)

    storage_backend_initialised = True

