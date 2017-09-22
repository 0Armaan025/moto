from __future__ import unicode_literals
from urllib.parse import urlsplit
import json
import six
import datetime

from moto.core.responses import BaseResponse
from moto.core.utils import camelcase_to_underscores, method_names_from_class
from werkzeug.exceptions import HTTPException

from .models import xray_backends
from .exceptions import AWSError, BadSegmentException


class XRayResponse(BaseResponse):

    def _error(self, code, message):
        return json.dumps({'__type': code, 'message': message}), dict(status=400)

    @property
    def xray_backend(self):
        return xray_backends[self.region]

    @property
    def request_params(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return {}

    def _get_param(self, param, default=None):
        return self.request_params.get(param, default)

    def call_action(self):
        # Amazon is just calling urls like /TelemetryRecords etc...
        action = urlsplit(self.uri).path.lstrip('/')
        action = camelcase_to_underscores(action)
        headers = self.response_headers
        method_names = method_names_from_class(self.__class__)
        if action in method_names:
            method = getattr(self, action)
            try:
                response = method()
            except HTTPException as http_error:
                response = http_error.description, dict(status=http_error.code)
            if isinstance(response, six.string_types):
                return 200, headers, response
            else:
                body, new_headers = response
                status = new_headers.get('status', 200)
                headers.update(new_headers)
                # Cast status to string
                if "status" in headers:
                    headers['status'] = str(headers['status'])
                return status, headers, body

        raise NotImplementedError(
            "The {0} action has not been implemented".format(action))

    # PutTelemetryRecords
    def telemetry_records(self):
        try:
            self.xray_backend.add_telemetry_records(self.request_params)
        except AWSError as err:
            return err.response()

        return ''

    # PutTraceSegments
    def trace_segments(self):
        docs = self._get_param('TraceSegmentDocuments')

        if docs is None:
            msg = 'Parameter TraceSegmentDocuments is missing'
            return json.dumps({'__type': 'MissingParameter', 'message': msg}), dict(status=400)

        # Raises an exception that contains info about a bad segment,
        # the object also has a to_dict() method
        bad_segments = []
        for doc in docs:
            try:
                self.xray_backend.process_segment(doc)
            except BadSegmentException as bad_seg:
                bad_segments.append(bad_seg)
            except Exception as err:
                return json.dumps({'__type': 'InternalFailure', 'message': str(err)}), dict(status=500)

        result = {'UnprocessedTraceSegments': [x.to_dict() for x in bad_segments]}
        return json.dumps(result)

    # GetTraceSummaries
    def trace_summaries(self):
        start_time = self._get_param('StartTime')
        end_time = self._get_param('EndTime')
        if start_time is None:
            msg = 'Parameter StartTime is missing'
            return json.dumps({'__type': 'MissingParameter', 'message': msg}), dict(status=400)
        if end_time is None:
            msg = 'Parameter EndTime is missing'
            return json.dumps({'__type': 'MissingParameter', 'message': msg}), dict(status=400)

        filter_expression = self._get_param('FilterExpression')
        sampling = self._get_param('Sampling', 'false') == 'true'

        try:
            start_time = datetime.datetime.fromtimestamp(int(start_time))
            end_time = datetime.datetime.fromtimestamp(int(end_time))
        except ValueError:
            msg = 'start_time and end_time are not integers'
            return json.dumps({'__type': 'InvalidParameterValue', 'message': msg}), dict(status=400)
        except Exception as err:
            return json.dumps({'__type': 'InternalFailure', 'message': str(err)}), dict(status=500)

        try:
            result = self.xray_backend.get_trace_summary(start_time, end_time, filter_expression, sampling)
        except AWSError as err:
            return err.response()
        except Exception as err:
            return json.dumps({'__type': 'InternalFailure', 'message': str(err)}), dict(status=500)

        return json.dumps(result)

    # BatchGetTraces
    def traces(self):
        raise NotImplementedError()

    # GetServiceGraph
    def service_graph(self):
        raise NotImplementedError()

    # GetTraceGraph
    def trace_graph(self):
        raise NotImplementedError()
