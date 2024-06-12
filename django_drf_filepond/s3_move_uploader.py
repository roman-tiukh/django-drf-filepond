import os
import django_drf_filepond.drf_filepond_settings as filepond_settings
from storages.backends.s3boto3 import S3Boto3Storage

class S3MoveStorage(S3Boto3Storage):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
    
    def save(self, name, content, *args, **kwargs):

        print(f"saving {name} with {content.name}")

        cleaned_name = self._clean_name(name)
        name = self._normalize_name(cleaned_name)
        print(self.bucket.name)
        self.bucket.Object(name).copy_from(CopySource={'Bucket': self.bucket.name, "Key": content.name})
        return cleaned_name
