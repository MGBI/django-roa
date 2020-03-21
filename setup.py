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
    # test with: $ pip2 install -e ./
    requires=[
        'Django',
        'requests',
        'wsgiref',
        'simplejson',
        'djangorestframework',
        'djangorestframework-xml',
        'djangorestframework-yaml'
    ]
else:
    # test with: $ pip3 install -e ./
    requires=[
        'Django',
        'requests',
        'simplejson',
        'djangorestframework',
        'djangorestframework-xml@git+https://github.com/unchris/django-rest-framework-xml.git@python3django3',
        'djangorestframework-yaml@git+https://github.com/SnijderC/django-rest-framework-yaml.git@master'
    ]

setup(
    name='django-roa',
    version='3.0.1',
    url='https://github.com/MGBI/django-roa',
    download_url='https://github.com/MGBI/django-roa/archive/master.zip',
    license='BSD',
    description="Turn your models into remote resources that you can access through Django's ORM.",
    author='Jeroen Arnoldus',
    author_email='jeroen@repleo.nl',
    packages=find_packages(),
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Web Environment',
        'Framework :: Django',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: Python3',
        'Topic :: Internet :: WWW/HTTP',
    ],
    install_requires=requires,
    tests_require=[
        'django-piston',
    ]
)
