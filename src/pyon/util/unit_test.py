#! /usr/bin/env python

"""Unit test base class and utils"""

from copy import deepcopy
from mock import Mock, mocksignature, patch, DEFAULT
import unittest
from unittest import SkipTest
from zope.interface import implementedBy

from pyon.core.bootstrap import IonObject, bootstrap_pyon, get_service_registry, CFG
from pyon.util.containers import dict_merge, DotDict
from pyon.util.file_sys import FileSystem


bootstrap_pyon()

def func_names(cls):
    import types
    return [name for name, value in cls.__dict__.items() if
            isinstance(value, types.FunctionType)]

def pop_last_call(mock):
    if not mock.call_count:
        raise AssertionError('Cannot pop last call: call_count is 0')
    mock.call_args_list.pop()
    try:
        mock.call_args = mock.call_args_list[-1]
    except IndexError:
        mock.call_args = None
        mock.called = False
    mock.call_count -= 1


class UnitTestCase(unittest.TestCase):
    SkipTest = SkipTest

    def __init__(self, *args, **kwargs):
        unittest.TestCase.__init__(self, *args, **kwargs)
        self.addCleanup(self._file_sys_clean)

    def shortDescription(self):
        # @see http://www.saltycrane.com/blog/2012/07/how-prevent-nose-unittest-using-docstring-when-verbosity-2/
        return None

    # override __str__ and __repr__ behavior to show a copy-pastable nosetest name for tests
    #  pack.module:TestClassName.test_function_name
    def __repr__(self):
        name = self.id()
        name = name.split('.')
        return "%s ( %s )" % (name[-1], '.'.join(name[:-2]) + ":" + '.'.join(name[-2:]))
    __str__ = __repr__

    def _create_IonObject_mock(self, name):
        mock_ionobj = Mock(name='IonObject')
        def side_effect(_def, _dict=None, **kwargs):
            test_obj = IonObject(_def, _dict, **kwargs)
            test_obj._validate()
            return DEFAULT
        mock_ionobj.side_effect = side_effect
        patcher = patch(name, mock_ionobj)
        thing = patcher.start()
        self.addCleanup(patcher.stop)
        return thing

    def _file_sys_clean(self):
        FileSystem._clean(CFG)


    def _create_service_mock(self, service_name):
        # set self.clients if not already set
        clients = Mock(name='clients')
        base_service = get_service_registry().get_service_base(service_name)
        # Save it to use in test_verify_service
        self.base_service = base_service
        self.addCleanup(delattr, self, 'base_service')
        dependencies = base_service.dependencies
        for dep_name in dependencies:
            dep_service = get_service_registry().get_service_base(dep_name)
            # Force mock service to use interface
            mock_service = Mock(name='clients.%s' % dep_name,
                    spec=dep_service)
            setattr(clients, dep_name, mock_service)
            # set self.dep_name for convenience
            setattr(self, dep_name, mock_service)
            self.addCleanup(delattr, self, dep_name)
            iface = list(implementedBy(dep_service))[0]
            names_and_methods = iface.namesAndDescriptions()
            for func_name, _ in names_and_methods:
                mock_func = mocksignature(getattr(dep_service, func_name),
                        mock=Mock(name='clients.%s.%s' % (dep_name,
                            func_name)), skipfirst=True)
                setattr(mock_service, func_name, mock_func)
        return clients

    # # Assuming your service is the only subclass of the Base Service
    # def test_verify_service(self):
    #     if not getattr(self, 'base_service', None):
    #         raise unittest.SkipTest('Not implementing an Ion Service')
    #     from zope.interface.verify import verifyClass
    #     base_service = self.base_service
    #     implemented_service = base_service.__subclasses__()[0]
    #     iface = list(implementedBy(base_service))[0]
    #     verifyClass(iface, implemented_service)
    #     # Check if defined functions in Base Service are all implemented
    #     difference = set(func_names(base_service)) - set(func_names(implemented_service)) - set(['__init__'])
    #     if difference:
    #         self.fail('Following function declarations in %s do not exist in %s : %s' %
    #                 (iface, implemented_service,
    #                     list(difference)))

    def _breakpoint(self, scope=None, global_scope=None):
        from pyon.util.breakpoint import breakpoint
        breakpoint(scope=scope, global_scope=global_scope)

    @staticmethod
    def _get_alt_cfg(cfg_merge):
        cfg_clone = deepcopy(CFG)
        dict_merge(cfg_clone, cfg_merge, inplace=True)
        return DotDict(**cfg_clone)

    def patch_alt_cfg(self, cfg_obj_or_str, cfg_merge):
        """Patches given CFG (DotDict) based on system CFG with given dict merged"""
        alt_cfg = self._get_alt_cfg(cfg_merge)
        self.patch_cfg(cfg_obj_or_str, alt_cfg)

    def patch_cfg(self, cfg_obj_or_str, *args, **kwargs):
        """
        Helper method for patching the CFG (or any dict, but useful for patching CFG).

        This method exists because the decorator versions of patch/patch.dict do not function
        until the test_ method is called - ie, when setUp is run, the patch hasn't occured yet.
        Use this in your setUp method if you need to patch CFG and have stuff in setUp respect it.

        @param  cfg_obj_or_str  An actual ref to CFG or a string defining where to find it ie 'pyon.ion.exchange.CFG'
        @param  *args           *args to pass to patch.dict
        @param  **kwargs        **kwargs to pass to patch.dict
        """
        patcher = patch.dict(cfg_obj_or_str, *args, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

# Aliases
IonUnitTestCase = UnitTestCase
PyonTestCase = UnitTestCase
