from enum import Enum

from aplus_auth import settings as auth_settings
from aplus_auth.auth.django import Request
from aplus_auth.payload import Permission
from django.db import models


class Course(models.Model):
    '''
    A course repository served out for learning environments.
    '''

    key = models.SlugField(unique=True)
    # course instance id on A+
    remote_id = models.IntegerField(unique=True, blank=True, null=True)
    git_origin = models.CharField(blank=True, max_length=255)
    git_branch = models.CharField(max_length=40)
    update_hook = models.URLField(blank=True)
    email_on_error = models.BooleanField(default=True)
    update_automatically = models.BooleanField(default=True)

    class META:
        ordering = ['key']

    def has_access(self, request: Request, permission: Permission, default: bool = False) -> bool:
        if self.remote_id is None:
            return default

        if auth_settings().DISABLE_LOGIN_CHECKS:
            return True

        if not hasattr(request, "auth") or request.auth is None:
            return False

        return request.auth.permissions.instances.has(permission, id=self.remote_id)

    def has_write_access(self, request: Request, default: bool = False):
        return self.has_access(request, Permission.WRITE, default)

    def has_read_access(self, request: Request, default: bool = False):
        return self.has_access(request, Permission.READ, default)


class UpdateStatus(Enum):
    PENDING="PENDING"
    RUNNING="RUNNING"
    SUCCESS="SUCCESS"
    FAILED="FAILED"
    SKIPPED="SKIPPED"


class CourseUpdate(models.Model):
    '''
    An update to course repo from the origin.
    '''
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='updates')
    request_ip = models.CharField(max_length=40)
    request_time = models.DateTimeField(auto_now_add=True)
    updated_time = models.DateTimeField(default=None, null=True, blank=True)
    status = models.CharField(max_length=10, default=UpdateStatus.PENDING, choices=[(tag, tag.value) for tag in UpdateStatus])
    log = models.TextField(default=None, null=True, blank=True)

    class META:
        ordering = ['-request_time']

