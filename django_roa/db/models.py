import sys
import copy
import logging
from io import BytesIO

import django

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned, \
    FieldError
from django.db import models
from django.db.models import signals
from django.db.models.options import Options
from django.apps import apps
from django.db.models.base import ModelBase, subclass_exception, method_get_order, method_set_order
from django.db.models.fields.related import (OneToOneField, lazy_related_operation)

from functools import update_wrapper

from django.utils.encoding import force_text
from rest_framework.parsers import JSONParser
from rest_framework_yaml.parsers import YAMLParser
from rest_framework_xml.parsers import XMLParser
from rest_framework.renderers import JSONRenderer
from rest_framework_yaml.renderers import YAMLRenderer
from rest_framework_xml.renderers import XMLRenderer

from django_roa.db import get_roa_headers, get_roa_client
from django_roa.db.exceptions import ROAException

from requests.exceptions import HTTPError

logger = logging.getLogger("django_roa")


DJANGO_LT_1_7 = django.VERSION[:2] < (1, 7)
DJANGO_GT_1_4 = django.VERSION[:2] > (1, 4)
PYTHON_LT_3_3 = sys.version_info<(3,3,0)

ROA_ARGS_NAMES_MAPPING = getattr(settings, 'ROA_ARGS_NAMES_MAPPING', {})
ROA_FORMAT = getattr(settings, 'ROA_FORMAT', 'json')
ROA_FILTERS = getattr(settings, 'ROA_FILTERS', {})
ROA_MODEL_NAME_MAPPING = getattr(settings, 'ROA_MODEL_NAME_MAPPING', [])
ROA_MODEL_CREATE_MAPPING = getattr(settings, 'ROA_MODEL_CREATE_MAPPING', {})
ROA_MODEL_UPDATE_MAPPING = getattr(settings, 'ROA_MODEL_UPDATE_MAPPING', {})
ROA_CUSTOM_ARGS = getattr(settings, "ROA_CUSTOM_ARGS", {})
ROA_SSL_CA = getattr(settings, 'ROA_SSL_CA', None)

DEFAULT_CHARSET = getattr(settings, 'DEFAULT_CHARSET', 'utf-8')


# adapted from:
# https://github.com/django/django/blob/1.11/django/db/models/fields/related.py#L88
def add_lazy_relation(cls, field, relation, operation):
    # Rearrange args for new Apps.lazy_model_operation

    def function(local, related, field):
        return operation(field, related, local)

    lazy_related_operation(function, cls, relation, field=field)


# copied from:
# https://github.com/django/django/blob/1.11/django/utils/functional.py#L13
# You can't trivially replace this with `functools.partial` because this binds
# to classes and returns bound instances, whereas functools.partial (on
# CPython) is a type and its instances don't bind.
def curry(_curried_func, *args, **kwargs):
    def _curried(*moreargs, **morekwargs):
        return _curried_func(*(args + moreargs), **dict(kwargs, **morekwargs))
    return _curried


class ROAModelBase(ModelBase):
    def __new__(cls, name, bases, attrs):
        if DJANGO_LT_1_7:
            return cls._new_old_django(name, bases, attrs)
        else:
            return cls._new_recent_django(name, bases, attrs)

    @classmethod
    def _new_recent_django(cls, name, bases, attrs):
        """
        Exactly the same except the line with ``isinstance(b, ROAModelBase)``.
        """
        import warnings
        from django.apps.config import MODELS_MODULE_NAME
        from django.apps import apps

        super_new = super(ModelBase, cls).__new__

        # Also ensure initialization is only performed for subclasses of Model
        # (excluding Model class itself).
        parents = [b for b in bases if isinstance(b, ROAModelBase)]
        if not parents:
            return super_new(cls, name, bases, attrs)

        # Create the class.
        module = attrs.pop('__module__')
        new_class = super_new(cls, name, bases, {'__module__': module})
        attr_meta = attrs.pop('Meta', None)
        abstract = getattr(attr_meta, 'abstract', False)
        if not attr_meta:
            meta = getattr(new_class, 'Meta', None)
        else:
            meta = attr_meta
        base_meta = getattr(new_class, '_meta', None)

        # Look for an application configuration to attach the model to.
        app_config = apps.get_containing_app_config(module)

        if getattr(meta, 'app_label', None) is None:

            if app_config is None:
                # If the model is imported before the configuration for its
                # application is created (#21719), or isn't in an installed
                # application (#21680), use the legacy logic to figure out the
                # app_label by looking one level up from the package or module
                # named 'models'. If no such package or module exists, fall
                # back to looking one level up from the module this model is
                # defined in.

                # For 'django.contrib.sites.models', this would be 'sites'.
                # For 'geo.models.places' this would be 'geo'.

                msg = (
                    "Model class %s.%s doesn't declare an explicit app_label "
                    "and either isn't in an application in INSTALLED_APPS or "
                    "else was imported before its application was loaded. " %
                    (module, name))
                if abstract:
                    msg += "Its app_label will be set to None in Django 1.9."
                else:
                    msg += "This will no longer be supported in Django 1.9."
                warnings.warn(msg, RemovedInDjango19Warning, stacklevel=2)

                model_module = sys.modules[new_class.__module__]
                package_components = model_module.__name__.split('.')
                package_components.reverse()  # find the last occurrence of 'models'
                try:
                    app_label_index = package_components.index(MODELS_MODULE_NAME) + 1
                except ValueError:
                    app_label_index = 1
                kwargs = {"app_label": package_components[app_label_index]}

            else:
                kwargs = {"app_label": app_config.label}

        else:
            kwargs = {}

        new_class.add_to_class('_meta', Options(meta, **kwargs))
        if not abstract:
            new_class.add_to_class(
                'DoesNotExist',
                subclass_exception(
                    str('DoesNotExist'),
                    tuple(x.DoesNotExist for x in parents if hasattr(x, '_meta') and not x._meta.abstract) or (ObjectDoesNotExist,),
                    module,
                    attached_to=new_class))
            new_class.add_to_class(
                'MultipleObjectsReturned',
                subclass_exception(
                    str('MultipleObjectsReturned'),
                    tuple(x.MultipleObjectsReturned for x in parents if hasattr(x, '_meta') and not x._meta.abstract) or (MultipleObjectsReturned,),
                    module,
                    attached_to=new_class))
            if base_meta and not base_meta.abstract:
                # Non-abstract child classes inherit some attributes from their
                # non-abstract parent (unless an ABC comes before it in the
                # method resolution order).
                if not hasattr(meta, 'ordering'):
                    new_class._meta.ordering = base_meta.ordering
                if not hasattr(meta, 'get_latest_by'):
                    new_class._meta.get_latest_by = base_meta.get_latest_by

        is_proxy = new_class._meta.proxy

        # If the model is a proxy, ensure that the base class
        # hasn't been swapped out.
        if is_proxy and base_meta and base_meta.swapped:
            raise TypeError("%s cannot proxy the swapped model '%s'." % (name, base_meta.swapped))

        if getattr(new_class, '_default_manager', None):
            if not is_proxy:
                # Multi-table inheritance doesn't inherit default manager from
                # parents.
                new_class._default_manager = None
                new_class._base_manager = None
            else:
                # Proxy classes do inherit parent's default manager, if none is
                # set explicitly.
                new_class._default_manager = new_class._default_manager._copy_to_model(new_class)
                new_class._base_manager = new_class._base_manager._copy_to_model(new_class)

        # Add all attributes to the class.
        for obj_name, obj in list(attrs.items()):
            new_class.add_to_class(obj_name, obj)

        # All the fields of any type declared on this model
        new_fields = (
            new_class._meta.local_fields +
            new_class._meta.local_many_to_many +
            new_class._meta.private_fields
        )
        field_names = set(f.name for f in new_fields)

        # Basic setup for proxy models.
        if is_proxy:
            base = None
            for parent in [kls for kls in parents if hasattr(kls, '_meta')]:
                if parent._meta.abstract:
                    if parent._meta.fields:
                        raise TypeError("Abstract base class containing model fields not permitted for proxy model '%s'." % name)
                    else:
                        continue
                if base is not None:
                    raise TypeError("Proxy model '%s' has more than one non-abstract model base class." % name)
                else:
                    base = parent
            if base is None:
                raise TypeError("Proxy model '%s' has no non-abstract model base class." % name)
            new_class._meta.setup_proxy(base)
            new_class._meta.concrete_model = base._meta.concrete_model
        else:
            new_class._meta.concrete_model = new_class

        # Collect the parent links for multi-table inheritance.
        parent_links = {}
        for base in reversed([new_class] + parents):
            # Conceptually equivalent to `if base is Model`.
            if not hasattr(base, '_meta'):
                continue
            # Skip concrete parent classes.
            if base != new_class and not base._meta.abstract:
                continue
            # Locate OneToOneField instances.
            for field in base._meta.local_fields:
                if isinstance(field, OneToOneField):
                    parent_links[field.rel.to] = field

        # Do the appropriate setup for any model parents.
        for base in parents:
            original_base = base
            if not hasattr(base, '_meta'):
                # Things without _meta aren't functional models, so they're
                # uninteresting parents.
                continue

            parent_fields = base._meta.local_fields + base._meta.local_many_to_many
            # Check for clashes between locally declared fields and those
            # on the base classes (we cannot handle shadowed fields at the
            # moment).
            for field in parent_fields:
                if field.name in field_names:
                    raise FieldError(
                        'Local field %r in class %r clashes '
                        'with field of similar name from '
                        'base class %r' % (field.name, name, base.__name__)
                    )
            if not base._meta.abstract:
                # Concrete classes...
                base = base._meta.concrete_model
                if base in parent_links:
                    field = parent_links[base]
                elif not is_proxy:
                    attr_name = '%s_ptr' % base._meta.model_name
                    field = OneToOneField(base, name=attr_name,
                            auto_created=True, parent_link=True)
                    # Only add the ptr field if it's not already present;
                    # e.g. migrations will already have it specified
                    if not hasattr(new_class, attr_name):
                        new_class.add_to_class(attr_name, field)
                else:
                    field = None
                new_class._meta.parents[base] = field
            else:
                # .. and abstract ones.
                for field in parent_fields:
                    new_class.add_to_class(field.name, copy.deepcopy(field))

                # Pass any non-abstract parent classes onto child.
                new_class._meta.parents.update(base._meta.parents)

            # Inherit managers from the abstract base classes.
            new_class.copy_managers(base._meta.abstract_managers)

            # Proxy models inherit the non-abstract managers from their base,
            # unless they have redefined any of them.
            if is_proxy:
                new_class.copy_managers(original_base._meta.concrete_managers)

            # Inherit virtual fields (like GenericForeignKey) from the parent
            # class
            for field in base._meta.virtual_fields:
                if base._meta.abstract and field.name in field_names:
                    raise FieldError(
                        'Local field %r in class %r clashes '
                        'with field of similar name from '
                        'abstract base class %r' % (field.name, name, base.__name__)
                    )
                new_class.add_to_class(field.name, copy.deepcopy(field))

        if abstract:
            # Abstract base models can't be instantiated and don't appear in
            # the list of models for an app. We do the final setup for them a
            # little differently from normal models.
            attr_meta.abstract = False
            new_class.Meta = attr_meta
            return new_class

        new_class._prepare()
        new_class._meta.apps.register_model(new_class._meta.app_label, new_class)
        return new_class

    @classmethod
    def _new_old_django(cls, name, bases, attrs):
        """
        Exactly the same except the line with ``isinstance(b, ROAModelBase)`` and part delimited by 'ROA HACK'
        """
        super_new = super(ModelBase, cls).__new__

        # six.with_metaclass() inserts an extra class called 'NewBase' in the
        # inheritance tree: Model -> NewBase -> object. But the initialization
        # should be executed only once for a given model class.

        # attrs will never be empty for classes declared in the standard way
        # (ie. with the `class` keyword). This is quite robust.
        if name == 'NewBase' and attrs == {}:
            return super_new(cls, name, bases, attrs)

        # Also ensure initialization is only performed for subclasses of Model
        # (excluding Model class itself).
        parents = [b for b in bases if isinstance(b, ROAModelBase) and
                not (b.__name__ == 'NewBase' and b.__mro__ == (b, object))]
        if not parents:
            return super_new(cls, name, bases, attrs)

        # Create the class.
        module = attrs.pop('__module__')
        new_class = super_new(cls, name, bases, {'__module__': module})
        attr_meta = attrs.pop('Meta', None)
        abstract = getattr(attr_meta, 'abstract', False)
        if not attr_meta:
            meta = getattr(new_class, 'Meta', None)
        else:
            meta = attr_meta
        base_meta = getattr(new_class, '_meta', None)

        if getattr(meta, 'app_label', None) is None:
            # Figure out the app_label by looking one level up.
            # For 'django.contrib.sites.models', this would be 'sites'.
            model_module = sys.modules[new_class.__module__]
            kwargs = {"app_label": model_module.__name__.split('.')[-2]}
        else:
            kwargs = {}

        new_class.add_to_class('_meta', Options(meta, **kwargs))
        if not abstract:
            # ROA HACK:

            subclass_kwargs = {
                'name': str('DoesNotExist'),
                'parents': tuple(x.DoesNotExist for x in parents if hasattr(x, '_meta') and not x._meta.abstract)
                    or (ObjectDoesNotExist,),
                'module': module
            }
            if DJANGO_GT_1_4:
                subclass_kwargs['attached_to'] = new_class

            new_class.add_to_class('DoesNotExist', subclass_exception(**subclass_kwargs))

            subclass_kwargs = {
                'name': str('MultipleObjectsReturned'),
                'parents': tuple(x.MultipleObjectsReturned for x in parents if hasattr(x, '_meta') and not x._meta.abstract)
                    or (MultipleObjectsReturned,),
                'module': module
            }
            if DJANGO_GT_1_4:
                subclass_kwargs['attached_to'] = new_class

            new_class.add_to_class('MultipleObjectsReturned', subclass_exception(**subclass_kwargs))

            # END HACK

            if base_meta and not base_meta.abstract:
                # Non-abstract child classes inherit some attributes from their
                # non-abstract parent (unless an ABC comes before it in the
                # method resolution order).
                if not hasattr(meta, 'ordering'):
                    new_class._meta.ordering = base_meta.ordering
                if not hasattr(meta, 'get_latest_by'):
                    new_class._meta.get_latest_by = base_meta.get_latest_by

        is_proxy = new_class._meta.proxy

        # If the model is a proxy, ensure that the base class
        # hasn't been swapped out.
        if is_proxy and base_meta and base_meta.swapped:
            raise TypeError("%s cannot proxy the swapped model '%s'." % (name, base_meta.swapped))

        if getattr(new_class, '_default_manager', None):
            if not is_proxy:
                # Multi-table inheritance doesn't inherit default manager from
                # parents.
                new_class._default_manager = None
                new_class._base_manager = None
            else:
                # Proxy classes do inherit parent's default manager, if none is
                # set explicitly.
                new_class._default_manager = new_class._default_manager._copy_to_model(new_class)
                new_class._base_manager = new_class._base_manager._copy_to_model(new_class)

        # Bail out early if we have already created this class.
        m = apps.get_model(new_class._meta.app_label, name,
                      seed_cache=False, only_installed=False)
        if m is not None:
            return m

        # Add all attributes to the class.
        for obj_name, obj in list(attrs.items()):
            new_class.add_to_class(obj_name, obj)

        # All the fields of any type declared on this model
        new_fields = new_class._meta.local_fields + \
                     new_class._meta.local_many_to_many + \
                     new_class._meta.virtual_fields
        field_names = set([f.name for f in new_fields])

        # Basic setup for proxy models.
        if is_proxy:
            base = None
            for parent in [cls for cls in parents if hasattr(cls, '_meta')]:
                if parent._meta.abstract:
                    if parent._meta.fields:
                        raise TypeError("Abstract base class containing model fields not permitted for proxy model '%s'." % name)
                    else:
                        continue
                if base is not None:
                    raise TypeError("Proxy model '%s' has more than one non-abstract model base class." % name)
                else:
                    base = parent
            if base is None:
                raise TypeError("Proxy model '%s' has no non-abstract model base class." % name)
            if (new_class._meta.local_fields or
                    new_class._meta.local_many_to_many):
                raise FieldError("Proxy model '%s' contains model fields." % name)
            new_class._meta.setup_proxy(base)
            new_class._meta.concrete_model = base._meta.concrete_model
        else:
            new_class._meta.concrete_model = new_class

        # Do the appropriate setup for any model parents.
        o2o_map = dict([(f.rel.to, f) for f in new_class._meta.local_fields
                if isinstance(f, OneToOneField)])

        for base in parents:
            original_base = base
            if not hasattr(base, '_meta'):
                # Things without _meta aren't functional models, so they're
                # uninteresting parents.
                continue

            parent_fields = base._meta.local_fields + base._meta.local_many_to_many
            # Check for clashes between locally declared fields and those
            # on the base classes (we cannot handle shadowed fields at the
            # moment).
            for field in parent_fields:
                if field.name in field_names:
                    raise FieldError('Local field %r in class %r clashes '
                                     'with field of similar name from '
                                     'base class %r' %
                                        (field.name, name, base.__name__))
            if not base._meta.abstract:
                # Concrete classes...
                base = base._meta.concrete_model
                if base in o2o_map:
                    field = o2o_map[base]
                elif not is_proxy:
                    attr_name = '%s_ptr' % base._meta.model_name
                    field = OneToOneField(base, name=attr_name,
                            auto_created=True, parent_link=True)
                    new_class.add_to_class(attr_name, field)
                else:
                    field = None
                new_class._meta.parents[base] = field
            else:
                # .. and abstract ones.
                for field in parent_fields:
                    new_class.add_to_class(field.name, copy.deepcopy(field))

                # Pass any non-abstract parent classes onto child.
                new_class._meta.parents.update(base._meta.parents)

            # Inherit managers from the abstract base classes.
            new_class.copy_managers(base._meta.abstract_managers)

            # Proxy models inherit the non-abstract managers from their base,
            # unless they have redefined any of them.
            if is_proxy:
                new_class.copy_managers(original_base._meta.concrete_managers)

            # Inherit virtual fields (like GenericForeignKey) from the parent
            # class
            for field in base._meta.virtual_fields:
                if base._meta.abstract and field.name in field_names:
                    raise FieldError('Local field %r in class %r clashes '\
                                     'with field of similar name from '\
                                     'abstract base class %r' % \
                                        (field.name, name, base.__name__))
                new_class.add_to_class(field.name, copy.deepcopy(field))

        if abstract:
            # Abstract base models can't be instantiated and don't appear in
            # the list of models for an app. We do the final setup for them a
            # little differently from normal models.
            attr_meta.abstract = False
            new_class.Meta = attr_meta
            return new_class

        new_class._prepare()

        # BJA Not relevant???
        # register_models(new_class._meta.app_label, new_class)

        # Because of the way imports happen (recursively), we may or may not be
        # the first time this model tries to register with the framework. There
        # should only be one class for each model, so we always return the
        # registered version.
        return apps.get_model(new_class._meta.app_label, name,
                         seed_cache=False, only_installed=False)

    def _prepare(cls):
        """
        Creates some methods once self._meta has been populated.
        """
        opts = cls._meta
        opts._prepare(cls)

        if opts.order_with_respect_to:
            cls.get_next_in_order = curry(cls._get_next_or_previous_in_order, is_next=True)
            cls.get_previous_in_order = curry(cls._get_next_or_previous_in_order, is_next=False)

            # defer creating accessors on the foreign class until we are
            # certain it has been created
            def make_foreign_order_accessors(field, model, cls):
                setattr(
                    field.rel.to,
                    'get_%s_order' % cls.__name__.lower(),
                    curry(method_get_order, cls)
                )
                setattr(
                    field.rel.to,
                    'set_%s_order' % cls.__name__.lower(),
                    curry(method_set_order, cls)
                )
            add_lazy_relation(
                cls,
                opts.order_with_respect_to,
                opts.order_with_respect_to.rel.to,
                make_foreign_order_accessors
            )

        # Give the class a docstring -- its definition.
        if cls.__doc__ is None:
            cls.__doc__ = "%s(%s)" % (cls.__name__, ", ".join([f.attname for f in opts.fields]))

        if hasattr(cls, 'get_absolute_url'):
            cls.get_absolute_url = update_wrapper(curry(get_absolute_url, opts, cls.get_absolute_url),
                                                  cls.get_absolute_url)

        if hasattr(cls, 'get_resource_url_list'):
            cls.get_resource_url_list = staticmethod(curry(get_resource_url_list,
                                                           opts, cls.get_resource_url_list))

        if hasattr(cls, 'get_resource_url_count'):
            cls.get_resource_url_count = update_wrapper(curry(get_resource_url_count, opts, cls.get_resource_url_count),
                                                        cls.get_resource_url_count)

        if hasattr(cls, 'get_resource_url_detail'):
            cls.get_resource_url_detail = update_wrapper(curry(get_resource_url_detail, opts, cls.get_resource_url_detail),
                                                         cls.get_resource_url_detail)

        signals.class_prepared.send(sender=cls)


class ROAModel(models.Model, metaclass=ROAModelBase):
    """
    Model which access remote resources.
    """

    @classmethod
    def serializer(cls):
        """
        Return a like Django Rest Framework serializer class
        """
        raise NotImplementedError

    def get_renderer(self):
        """
        Cf from rest_framework.renderers import JSONRenderer
        """
        if ROA_FORMAT == 'json':
            return JSONRenderer()
        elif ROA_FORMAT == 'xml':
            return XMLRenderer()
        elif ROAException == 'yaml':
            return YAMLRenderer()
        else:
            raise NotImplementedError

    @classmethod
    def get_parser(cls):
        """
        Cf from rest_framework.parsers import JSONParser
        """
        if ROA_FORMAT == 'json':
            return JSONParser()
        elif ROA_FORMAT == 'xml':
            return XMLParser()
        elif ROAException == 'yaml':
            return YAMLParser()
        else:
            raise NotImplementedError

    def get_serializer_content_type(self):
        if ROA_FORMAT == 'json':
            return {'Content-Type' : 'application/json'}
        elif ROA_FORMAT == 'xml':
            return {'Content-Type' : 'application/xml'}
        elif ROAException == 'yaml':
            return {'Content-Type' : 'text/x-yaml'}
        else:
            raise NotImplementedError

    @classmethod
    def get_serializer(cls, instance=None, data=None, partial=False, **kwargs):
        """
        Transform API response to Django model objects.
        """
        serializer_class = cls.serializer()
        serializer = None

        if instance:
            serializer = serializer_class(instance, partial=partial, **kwargs)
        elif data:
            data = data['results'] if 'results' in data else data
            serializer = serializer_class(data=data, many=isinstance(data, list), **kwargs)

        return serializer

    @staticmethod
    def get_resource_url_list():
        raise Exception("Static method get_resource_url_list is not defined.")

    @classmethod
    def count_response(cls, data, **kwargs):
        """
        Read count query response and return result
        """
        if 'count' in data:            # with default DRF : with pagination
            count = int(data['count'])
        elif isinstance(data, (list, tuple)):
            count = len(data)          # with default DRF : without pagination
        else:
            count = int(data)
        return count

    def get_resource_url_count(self):
        # By default this method is not with compatible with json Django Rest Framework standard viewset urls
        # In this case, you just have to override it and return self.get_resource_url_list()
        return "%scount/" % (self.get_resource_url_list(),)

    def get_resource_url_detail(self):
        return "%s%s/" % (self.get_resource_url_list(), self.pk)

    def save_base(self, raw=False, cls=None, origin=None, force_insert=False,
                  force_update=False, using=None, update_fields=None):
        """
        Does the heavy-lifting involved in saving. Subclasses shouldn't need to
        override this method. It's separate from save() in order to hide the
        need for overrides of save() to pass around internal-only parameters
        ('raw', 'cls', and 'origin').
        """

        assert not (force_insert and force_update)

        record_exists = False

        if cls is None:
            cls = self.__class__
            meta = cls._meta
            if not meta.proxy:
                origin = cls
        else:
            meta = cls._meta

        if origin and not getattr(meta, "auto_created", False):
            signals.pre_save.send(sender=origin, instance=self, raw=raw)

        model_name = str(meta)

        # If we are in a raw save, save the object exactly as presented.
        # That means that we don't try to be smart about saving attributes
        # that might have come from the parent class - we just save the
        # attributes we have been given to the class we have been given.
        # We also go through this process to defer the save of proxy objects
        # to their actual underlying model.
        if not raw or meta.proxy:
            if meta.proxy:
                org = cls
            else:
                org = None
            for parent, field in list(meta.parents.items()):
                # At this point, parent's primary key field may be unknown
                # (for example, from administration form which doesn't fill
                # this field). If so, fill it.
                if field and getattr(self, parent._meta.pk.attname) is None and getattr(self, field.attname) is not None:
                    setattr(self, parent._meta.pk.attname, getattr(self, field.attname))

                self.save_base(cls=parent, origin=org, using=using)

                if field:
                    setattr(self, field.attname, self._get_pk_val(parent._meta))
            if meta.proxy:
                return

        if not meta.proxy:
            pk_val = self._get_pk_val(meta)
            pk_is_set = pk_val is not None

            get_args = {}
            get_args[ROA_ARGS_NAMES_MAPPING.get('FORMAT', 'format')] = ROA_FORMAT
            get_args.update(ROA_CUSTOM_ARGS)

            # Construct Json payload
            serializer = self.get_serializer(self)
            payload = self.get_renderer().render(serializer.data)

            # Add serializer content_type
            headers = get_roa_headers()
            headers.update(self.get_serializer_content_type())

            requests_client = get_roa_client()

            # check if resource use custom primary key
            if not meta.pk.attname in ['pk', 'id']:
                # consider it might be inserting so check it first
                # @todo: try to improve this block to check if custom pripary key is not None first

                try:

                    if ROA_SSL_CA:
                        response=requests_client.get(self.get_resource_url_detail(),params=None,headers=headers,verify=ROA_SSL_CA)
                    else:
                        response=requests_client.get(self.get_resource_url_detail(),params=None,headers=headers)
                    response=response.text.encode("utf-8")
                except HTTPError:
                    pk_is_set = False

            if force_update or pk_is_set and not self.pk is None:
                record_exists = True
                try:
                    logger.debug("""Modifying : "%s" through %s with payload "%s" and GET args "%s" """ % (
                                  force_text(self),
                                  force_text(self.get_resource_url_detail()),
                                  force_text(payload),
                                  force_text(get_args)))
                    if ROA_SSL_CA:
                        response=requests_client.put(self.get_resource_url_detail(),data=payload,headers=headers,verify=ROA_SSL_CA)
                    else:
                        response=requests_client.put(self.get_resource_url_detail(),data=payload,headers=headers)
                    response=response.text.encode("utf-8")
                except HTTPError as e:
                    raise ROAException(e)
            else:
                record_exists = False
                try:
                    logger.debug("""Creating  : "%s" through %s with payload "%s" and GET args "%s" """ % (
                                  force_text(self),
                                  force_text(self.get_resource_url_list()),
                                  force_text(payload),
                                  force_text(get_args)))
                    if ROA_SSL_CA:
                        response=requests_client.post(self.get_resource_url_list(),data=payload,headers=headers,verify=ROA_SSL_CA)
                    else:
                        response=requests_client.post(self.get_resource_url_list(),data=payload,headers=headers)
                    response=response.text.encode("utf-8")
                except HTTPError as e:
                    raise ROAException(e)

            data = self.get_parser().parse(BytesIO(response))
            serializer = self.get_serializer(data=data)

            for field in serializer.fields.items():
                validators = field[1].validators
                field[1].validators = []
                for validator in validators:
                    if validator.__class__.__name__ != "UniqueValidator":
                        field[1].validators.append(validator)

            if not serializer.is_valid():
                raise ROAException('Invalid deserialization for %s model: %s' % (self, serializer.errors))
            obj = serializer.Meta.model(**serializer.validated_data)
            try:
                self.pk = int(obj.pk)
            except ValueError:
                self.pk = obj.pk
            self = obj

        if origin:
            signals.post_save.send(sender=origin, instance=self,
                created=(not record_exists), raw=raw)

    save_base.alters_data = True

    def delete(self):
        assert self._get_pk_val() is not None, "%s object can't be deleted " \
                "because its %s attribute is set to None." \
                % (self._meta.object_name, self._meta.pk.attname)

        # Deletion in cascade should be done server side.

        logger.debug("""Deleting  : "%s" through %s""" % \
            (str(self), str(self.get_resource_url_detail())))

        # Add serializer content_type
        headers = get_roa_headers()
        headers.update(self.get_serializer_content_type())

        requests_client = get_roa_client()

        if ROA_SSL_CA:
            response=requests_client.delete(self.get_resource_url_detail(),headers=headers,verify=ROA_SSL_CA)
        else:
            response=requests_client.delete(self.get_resource_url_detail(),headers=headers)
        if response.status_code in [200, 202, 204]:
            self.pk = None

    delete.alters_data = True

    def _get_unique_checks(self, exclude=None):
        """
        We don't want to check unicity that way for now.
        """
        unique_checks, date_checks = [], []
        return unique_checks, date_checks


##############################################
# HELPER FUNCTIONS (CURRIED MODEL FUNCTIONS) #
##############################################

ROA_URL_OVERRIDES_LIST = getattr(settings, 'ROA_URL_OVERRIDES_LIST', {})
ROA_URL_OVERRIDES_COUNT = getattr(settings, 'ROA_URL_OVERRIDES_COUNT', {})
ROA_URL_OVERRIDES_DETAIL = getattr(settings, 'ROA_URL_OVERRIDES_DETAIL', {})


def get_resource_url_list(opts, func, *args, **kwargs):
    if DJANGO_LT_1_7:
        key = '%s.%s' % (opts.app_label, opts.module_name)
    else:
        key = '%s.%s' % (opts.app_label, opts.model_name)

    overridden = ROA_URL_OVERRIDES_LIST.get(key, False)
    return overridden and overridden or func(*args, **kwargs)


def get_resource_url_count(opts, func, self, *args, **kwargs):
    if DJANGO_LT_1_7:
        key = '%s.%s' % (opts.app_label, opts.module_name)
    else:
        key = '%s.%s' % (opts.app_label, opts.model_name)

    return ROA_URL_OVERRIDES_COUNT.get(key, func)(self, *args, **kwargs)


def get_resource_url_detail(opts, func, self, *args, **kwargs):
    if DJANGO_LT_1_7:
        key = '%s.%s' % (opts.app_label, opts.module_name)
    else:
        key = '%s.%s' % (opts.app_label, opts.model_name)

    return ROA_URL_OVERRIDES_DETAIL.get(key, func)(self, *args, **kwargs)
