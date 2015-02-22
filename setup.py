#!/usr/bin/env python
# -*- coding: utf-8 -*-

try:
    from setuptools import setup, find_packages
except ImportError:
    import ez_setup
    ez_setup.use_setuptools()
    from setuptools import setup, find_packages

import sys
if sys.version_info<(3,3,0):
    requires=[
        'Django',
        'requests',
        'wsgiref',
        'simplejson',
        'djangorestframework'
    ]
else:
    requires=[
        'Django',
        'requests',
        'simplejson',
        'djangorestframework'
    ]
    
setup(
    name='django-roa',
    version='1.8.37',
    url='https://github.com/charles-vdulac/django-roa',
    download_url='https://github.com/charles-vdulac/django-roa/archive/master.zip',
    license='BSD',
    description="Turn your models into remote resources that you can access through Django's ORM.",
    author='David Larlet',
    author_email='david@larlet.fr',
    packages=find_packages(),
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Web Environment',
        'Framework :: Django',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP',
    ],
    install_requires=requires,
    tests_require={
        'Piston-tests': ['django-piston'],
    }
)
