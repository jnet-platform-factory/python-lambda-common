"""JSON bodies in the shapes the portals expect."""

import datetime
import json


def datetime_str_converter(_datetime):
    if isinstance(_datetime, datetime.datetime):
        return _datetime.isoformat()
    return str(_datetime)


def body_data(data):
    return json.dumps({
        "data": data
    }, default=datetime_str_converter)


def plain_body(data):
    return json.dumps(data, default=datetime_str_converter)


def paginated(data, **kwargs):
    if isinstance(data, list):
        data = {
            "data": data
        }
    return json.dumps({
        "total_items": data.get('total_items'),
        "page": data.get('page'),
        "limit": data.get('limit'),
        "total_pages": data.get('total_pages'),
        "next_page": data.get('next_page'),
        "previous_page": data.get('previous_page'),
        "count": data.get('count'),
        "data": data.get('data', []),
        **kwargs
    }, default=datetime_str_converter)


def message(details, data=None, status="Success"):
    return json.dumps({
        "message": status,
        "show_toast": False,
        "message_detail": details,
        "data": data
    }, default=datetime_str_converter)


def toast_message(details, data=None, status="Success", **kwargs):
    return json.dumps({
        "message": status,
        "show_toast": kwargs.get('show_toast', True),
        "message_detail": details,
        "data": data
    }, default=datetime_str_converter)
