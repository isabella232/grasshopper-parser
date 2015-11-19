"""
Flask-based REST API for parsing address string into its component parts
"""
from datetime import datetime
from flask import Flask, jsonify, request
import platform
import pytz
import usaddress
import yaml

UP_SINCE = datetime.now(pytz.utc).isoformat()
HOSTNAME = platform.node()
MAX_BATCH_SIZE = 5000

with open("rules.yaml", 'r') as f:
    rules_yaml= yaml.safe_load(f)


STANDARD_PART_MAPPING = {x['value']: x['id'] for x in rules_yaml['address_parts']['standard']}
AGGREGATE_PART_MAPPING = {x['id']: x['parts'] for x in rules_yaml['address_parts']['aggregates']}
PROFILE_MAPPING = {x['id']: x['required'] for x in rules_yaml['profiles']}

def parse_with_parse(addr_str):
    """
    Translates an address string using usaddress's `parse()` function

    See: http://usaddress.readthedocs.org/en/latest/#usage
    """
    # usaddress parses free-text address into an array of tuples.
    parsed = usaddress.parse(addr_str)

    addr_parts = [{'type': STANDARD_PART_MAPPING[v], 'value': k} for k,v in parsed]
        
    return addr_parts


def parse_with_tag(addr_str):
    """
    Translates an address string using usaddress's `tag()` function

    See: http://usaddress.readthedocs.org/en/latest/#usage
    """
    try:
        # `tag` returns OrderedDict, ordered by address parts in original address string
        tagged = usaddress.tag(addr_str)[0].items()
    except usaddress.RepeatedLabelError:
        
        # FIXME: Add richer logging here with contents of `rle` or chain exception w/ Python 3
        raise InvalidApiUsage("Could not parse address '{}' with 'tag' method".format(addr_str))

    addr_parts = [{'type': STANDARD_PART_MAPPING[k], 'value': v} for k,v in tagged]

    return addr_parts


# Maps `method` param to corresponding parse function
parse_method_dispatch = {'parse': parse_with_parse, 'tag': parse_with_tag}

def add_profile_addr_parts(profile_name, addr_parts):
    """
    Translates the address parts to profile-specific parts
    """
    try:
        profile_part_types = PROFILE_MAPPING[profile_name]
    except KeyError:
        raise InvalidApiUsage("Parsing profile '{}' not supported".format(profile_name))

    # Filter out the "standard" address part types
    aggr_part_types = filter(lambda x: AGGREGATE_PART_MAPPING.has_key(x), profile_part_types)

    for aggr_part_type in aggr_part_types:
        # Get all child address parts types for a given aggregate address part type
        child_part_types = AGGREGATE_PART_MAPPING[aggr_part_type]
        filtered_child_parts = filter(lambda x: x['type'] in child_part_types, addr_parts)
        child_part_values = map(lambda x: x['value'], filtered_child_parts)
        
        aggr_part_value = " ".join(child_part_values)

        addr_parts.append({'type': aggr_part_type, 'value': aggr_part_value})
        
    return addr_parts


class InvalidApiUsage(Exception):
    """
    Exception for invalid usage of address parsing API

    This is a simplifiled version of Flask's Implementing API Exceptions:
    See: http://flask.pocoo.org/docs/0.10/patterns/apierrors/
    """
    status_code = 400

    def __init__(self, message, status_code=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


app = Flask(__name__)


@app.route('/', methods=['GET'])
def status():
    """
    Provides the current status of the address parsing service
    """

    status = {
        "service": "grasshopper-parser",
        "status": "OK",
        "time": datetime.now(pytz.utc).isoformat(),
        "host": HOSTNAME,
        "upSince": UP_SINCE,
    }

    return jsonify(status)


@app.route('/parse', methods=['GET'])
def parse():
    """
    Parses an address string into its component parts
    """
    params = request.args

    try:
        addr_str = params['address']
    except KeyError:
        raise InvalidApiUsage("'address' query param is required.")

    method = params.get('method', 'tag')
    profile = params.get('profile', None)

    try:
        addr_parts = parse_method_dispatch[method](addr_str)
    except KeyError:
        raise InvalidApiUsage("Parsing method '{}' not supported.".format(method))
        
    if profile:
        addr_parts = add_profile_addr_parts(profile, addr_parts)

    response = {
        'input': addr_str,
        'parts': addr_parts
    }

    return jsonify(response)


@app.route('/parse', methods=['POST'])
def parse_batch():
    """
    Parses a batch of address strings into the component parts
    """
    # FIXME: Remove "force", add explicit Content-Type handling
    body = request.get_json(force=True)

    method = body.get('method', 'tag')
    profile = body.get('profile', None)
    addresses = body.get('addresses', None)

    if not addresses:
        raise InvalidApiUsage("'addresses' array not populated")

    addrs_len = len(addresses)

    if addrs_len > MAX_BATCH_SIZE:
        raise InvalidApiUsage("'addresses' contained {} elements, exceeding max of {}".format(addrs_len, MAX_BATCH_SIZE))

    parsed = []
    failed = []
    
    for addr_str in addresses:
        try:
            addr_parts = parse_method_dispatch[method](addr_str)

            if profile:
                addr_parts = add_profile_addr_parts(profile, addr_parts)

        except KeyError:
            raise InvalidApiUsage("Parsing method '{}' not supported.".format(method))
        except InvalidApiUsage:
            #FIXME: Logger does not work under Gunicorn
            app.logger.warn('Could not parse address "{}"'.format(addr_str))
            failed.append(addr_str)

        parsed.append({
            'input': addr_str,
            'parts': addr_parts
        })

    response = {
        'parsed': parsed, 
        'failed': failed
    }

    return jsonify(response)


def gen_error_json(message, code):
    return jsonify({'error': message, 'statusCode': code}), code


@app.errorhandler(InvalidApiUsage)
def usage_error(error):
    return gen_error_json(error.message, error.status_code)


@app.errorhandler(404)
def not_found_error(error):
    return gen_error_json('Resource not found', 404)


@app.errorhandler(Exception)
def default_error(error):
    # FIXME: This should be scrubbed
    app.logger.exception('Internal server error')
    return gen_error_json('Internal server error', 500)


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
