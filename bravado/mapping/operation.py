import logging

import requests.models

from bravado.mapping.unmarshal import unmarshal_schema_object
from bravado.response import HTTPFuture, RequestsLibResponseAdapter
from bravado.exception import SwaggerError
from bravado.mapping.param import Param, marshal_param

log = logging.getLogger(__name__)


class Operation(object):
    """Perform a request by taking the kwargs passed to the call and
    constructing an HTTP request.

    :type swagger_spec: :class:`Spec`
    :param path_name: path of the operation. e.g. /pet/{petId}
    :param http_method: get/put/post/delete/etc
    :param op_spec: operation specification in dict form
    """
    def __init__(self, swagger_spec, path_name, http_method, op_spec):
        self.swagger_spec = swagger_spec
        self.path_name = path_name
        self.http_method = http_method
        self.op_spec = op_spec

        # generated by @property when necessary since this is optional.
        # Diverges from op_* naming scheme since it is called 'operation_id'
        # in the Swagger 2.0 Spec.
        self._operation_id = None

        # (key, value) = (param name, Param)
        self.params = {}

    @classmethod
    def from_spec(cls, swagger_spec, path_name, http_method, op_spec):
        """
        Creates a :class:`Operation` and builds up its list of :class:`Param`s

        :param swagger_spec: :class:`Spec`
        :param path_name: path of the operation. e.g. /pet/{petId}
        :param http_method: get/put/post/delete/etc
        :param op_spec: operation specification in dict form
        :rtype: :class:`Operation`
        """
        op = cls(swagger_spec, path_name, http_method, op_spec)
        op.build_params()
        return op

    def build_params(self):
        """
        Builds up the list of this operations parameters taking into account
        parameters that may be available for this operation's path component.
        """
        # TODO: factory method
        self.params = {}
        op_param_specs = self.op_spec.get('parameters', [])
        path_specs = self.swagger_spec.spec_dict['paths'][self.path_name]
        path_param_specs = path_specs.get('parameters', [])
        param_specs = op_param_specs + path_param_specs

        for param_spec in param_specs:
            param = Param(self.swagger_spec, param_spec)
            self.params[param.name] = param

    @property
    def operation_id(self):
        """A friendly name for the operation. The id MUST be unique among all
        operations described in the API. Tools and libraries MAY use the
        operation id to uniquely identify an operation.

        This this field is not required, it will be generated when needed.

        :rtype: str
        """
        if self._operation_id is None:
            self._operation_id = self.op_spec.get('operationId')
            if self._operation_id is None:
                # build based on the http method and request path
                self._operation_id = (self.http_method + '_' + self.path_name)\
                    .replace('/', '_')\
                    .replace('{', '_')\
                    .replace('}', '_')\
                    .replace('__', '_')\
                    .strip('_')
        return self._operation_id

    def __repr__(self):
        return u"%s(%s)" % (self.__class__.__name__, self.operation_id)

    def construct_request(self, **kwargs):
        """
        :param kwargs: parameter name/value pairs to pass to the invocation of
            the operation
        :return: request in dict form
        """
        request_options = kwargs.pop('_request_options', {})
        request = {
            'method': self.http_method,
            'url': self.swagger_spec.api_url + self.path_name,
            'params': {},
            'headers': request_options.get('headers', {}),
        }
        self.construct_params(request, kwargs)
        return request

    def construct_params(self, request, op_kwargs):
        """
        Given the parameters passed to the operation invocation, validates and
        marshals the parmameters into the request dict.

        :type request: dict
        :param op_kwargs: the kwargs passed to the operation invocation
        :raises: TypeError on extra parameters or when a required parameter
            is not supplied.
        """
        current_params = self.params.copy()
        for param_name, param_value in op_kwargs.iteritems():
            param = current_params.pop(param_name, None)
            if param is None:
                raise TypeError("{0} does not have parameter {1}".format(
                    self.operation_id, param_name))
            marshal_param(param, param_value, request)

        # Check required params and non-required params with a 'default' value
        for remaining_param in current_params.itervalues():
            if remaining_param.required:
                raise TypeError(
                    '{0} is a required parameter'.format(remaining_param.name))
            if not remaining_param.required and remaining_param.has_default():
                marshal_param(remaining_param, None, request)

    def __call__(self, **kwargs):
        log.debug(u"%s(%s)" % (self.operation_id, kwargs))
        request = self.construct_request(**kwargs)

        def response_future(response, **kwargs):
            return handle_response(response, self, **kwargs)

        return HTTPFuture(
            self.swagger_spec.http_client, request, response_future)


def handle_response(response, op):
    """Process the response from the given operation invocation's request.

    :type response: 3rd party library http response object
          :class:`requests.models.Response`  or
          :class:`fido.fido.Response`
    :type op: :class:`bravado.mapping.operation.Operation`
    :returns: tuple (status_code, response value) where type(response value)
        is one of None, python primitive, list, object, or Model.
    """
    if isinstance(response, requests.models.Response):
        wrapped_response = RequestsLibResponseAdapter(response)
    else:
        # TODO: Fix as part of SRV-1454 for fido
        raise NotImplementedError(
            'TODO: Handle response of type {0}'.format(type(response)))

    response_spec = get_response_spec(wrapped_response.status_code, op)
    return unmarshal_response(op.swagger_spec, response_spec, wrapped_response)


def unmarshal_response(swagger_spec, response_spec, response):
    """Unmarshal the http response into a (status_code, value) based on the
    response specification.

    :type swagger_spec: :class:`bravado.mapping.spec.Spec`
    :param response_spec: response specification in dict form
    :type response: :class:`bravado.mapping.response.ResponseLike`
    :returns: tuple of (status_code, value) where type(value) matches
        response_spec['schema']['type'] if it exists, None otherwise.
    """
    def has_content(response_spec):
        return 'schema' in response_spec

    if not has_content(response_spec):
        return response.status_code, None

    # TODO: Non-json response contents
    content_spec = response_spec['schema']
    content_value = response.json()
    return response.status_code, unmarshal_schema_object(
        swagger_spec, content_spec, content_value)


def get_response_spec(status_code, op):
    """Given the http status_code of an operation invocation's response, figure
    out which response specification it maps to.

    #/paths/
        {path_name}/
            {http_method}/
                responses/
                    {status_code}/
                        {response}

    :type status_code: int
    :type op: :class:`bravado.mapping.operation.Operation`
    :return: response specification
    :rtype: dict
    :raises: SwaggerError when the status_code could not be mapped to a response
        specification.
    """
    # We don't need to worry about checking #/responses/ because jsonref has
    # already inlined the $refs
    response_specs = op.op_spec.get('responses')
    default_response_spec = response_specs.get('default', None)
    response_spec = response_specs.get(str(status_code), default_response_spec)
    if response_spec is None:
        raise SwaggerError(
            "Response specification matching http status_code {0} not found "
            "for {1}. Either add a response specifiction for the status_code "
            "or use a `default` response.".format(op, status_code))
    return response_spec
