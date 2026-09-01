from django.db import models

from base.models import BaseModel


class Company(BaseModel):
    name = models.CharField(max_length=255, unique=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.name
