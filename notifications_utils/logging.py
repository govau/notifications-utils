from itertools import product
import re
import os
import sys

from flask import request, current_app, g
from flask.ctx import has_request_context
from pythonjsonlogger.jsonlogger import JsonFormatter as BaseJSONFormatter
from time import monotonic

import logging

LOG_FORMAT = '%(asctime)s %(app_name)s %(name)s %(levelname)s ' \
             '%(request_id)s "%(message)s" [in %(pathname)s:%(lineno)d]'
TIME_FORMAT = '%Y-%m-%dT%H:%M:%S'

logger = logging.getLogger(__name__)


def build_log_line(extra_fields):
    fields = []
    if 'service_id' in extra_fields:
        fields.append(str(extra_fields.get('service_id')))
    standard_fields = [extra_fields.get('method'), extra_fields.get('url'), extra_fields.get('status')]
    fields += [str(field) for field in standard_fields if field is not None]
    if 'time_taken' in extra_fields:
        fields.append(extra_fields.get('time_taken'))
    return ' '.join(fields)


def build_statsd_line(extra_fields):
    fields = []
    if 'service_id' in extra_fields:
        if extra_fields.get('service_id') == 'notify-admin':
            fields = [str(extra_fields.get('service_id'))]
        else:
            fields = ["service-id", str(extra_fields.get('service_id'))]
    standard_fields = [extra_fields.get('method'), extra_fields.get('endpoint'), extra_fields.get('status')]
    fields += [str(field) for field in standard_fields if field is not None]
    return '.'.join(fields)


def init_app(app, statsd_client=None):
    app.config.setdefault('NOTIFY_LOG_LEVEL', 'INFO')
    app.config.setdefault('NOTIFY_APP_NAME', 'none')
    app.config.setdefault('NOTIFY_LOG_PATH', './log/application.log')

    @app.after_request
    def after_request(response):
        extra_fields = {
            'method': request.method,
            'url': request.url,
            'status': response.status_code
        }

        if 'service_id' in g:
            extra_fields.update({
                'service_id': g.service_id
            })

        if 'start' in g:
            time_taken = monotonic() - g.start
            extra_fields.update({
                'time_taken': "%.5f" % time_taken
            })

        if 'endpoint' in g:
            extra_fields.update({
                'endpoint': g.endpoint
            })

        if statsd_client:
            stat = build_statsd_line(extra_fields)
            statsd_client.incr(stat)

            if 'time_taken' in extra_fields:
                statsd_client.timing(stat, time_taken)

        current_app.logger.info(build_log_line(extra_fields))
        return response

    logging.getLogger().addHandler(logging.NullHandler())

    del app.logger.handlers[:]

    handlers = get_handlers(app)
    loglevel = logging.getLevelName(app.config['NOTIFY_LOG_LEVEL'])
    loggers = [app.logger, logging.getLogger('utils')]
    for l, handler in product(loggers, handlers):
        l.addHandler(handler)
        l.setLevel(loglevel)
    logging.getLogger('boto3').setLevel(logging.WARNING)
    app.logger.info("Logging configured")


def get_handlers(app):
    handlers = []
    standard_formatter = CustomLogFormatter(LOG_FORMAT, TIME_FORMAT)
    json_formatter = JSONFormatter(LOG_FORMAT, TIME_FORMAT)

    if app.debug:
        handler = logging.StreamHandler(sys.stderr)
        handlers.append(configure_handler(handler, app, standard_formatter))
    elif app.config['LOGGING_STDOUT_JSON']:
        handler = logging.StreamHandler(sys.stdout)
        handlers.append(configure_handler(handler, app, json_formatter))
    else:
        handler = logging.FileHandler(app.config['NOTIFY_LOG_PATH'])
        handlers.append(configure_handler(handler, app, standard_formatter))

        handler = logging.FileHandler(app.config['NOTIFY_LOG_PATH'] + '.json')
        handlers.append(configure_handler(handler, app, json_formatter))

    return handlers


def configure_handler(handler, app, formatter):
    handler.setLevel(logging.getLevelName(app.config['NOTIFY_LOG_LEVEL']))
    handler.setFormatter(formatter)
    handler.addFilter(AppNameFilter(app.config['NOTIFY_APP_NAME']))
    handler.addFilter(RequestIdFilter())

    return handler


class AppNameFilter(logging.Filter):
    def __init__(self, app_name):
        self.app_name = app_name

    def filter(self, record):
        record.app_name = self.app_name

        return record


class RequestIdFilter(logging.Filter):
    @property
    def request_id(self):
        if has_request_context() and hasattr(request, 'request_id'):
            return request.request_id
        else:
            return 'no-request-id'

    def filter(self, record):
        record.request_id = self.request_id

        return record


class CustomLogFormatter(logging.Formatter):
    """Accepts a format string for the message and formats it with the extra fields"""

    FORMAT_STRING_FIELDS_PATTERN = re.compile(r'\((.+?)\)', re.IGNORECASE)

    def add_fields(self, record):
        for field in self.FORMAT_STRING_FIELDS_PATTERN.findall(self._fmt):
            record.__dict__[field] = record.__dict__.get(field)
        return record

    def format(self, record):
        record = self.add_fields(record)
        try:
            record.msg = str(record.msg).format(**record.__dict__)
        except KeyError as e:
            logger.exception("failed to format log message: {} not found".format(e))
        return super(CustomLogFormatter, self).format(record)


class JSONFormatter(BaseJSONFormatter):
    def process_log_record(self, log_record):
        rename_map = {
            "asctime": "time",
            "request_id": "requestId",
            "app_name": "application",
        }
        for key, newkey in rename_map.items():
            log_record[newkey] = log_record.pop(key)
        log_record['logType'] = "application"
        try:
            log_record['message'] = log_record['message'].format(**log_record)
        except KeyError as e:
            logger.exception("failed to format log message: {} not found".format(e))
        return log_record
