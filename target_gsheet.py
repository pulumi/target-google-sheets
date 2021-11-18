#!/usr/bin/env python3

import argparse
import functools
import io
import os
import sys
import json
import logging
import collections
import threading
import http.client
import urllib
import pkg_resources

from jsonschema import validate
import singer

import httplib2

from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

# Read the config
try:
    parser = argparse.ArgumentParser(parents=[tools.argparser])
    parser.add_argument('-c', '--config', help='Config file', required=True)
    flags = parser.parse_args()
except ImportError:
    flags = None

logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
logger = singer.get_logger()


def get_credentials(config):
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.

    Returns:
        Credentials, the obtained credential.
    """
    credentials = client.AccessTokenCredentials(config['access_token'], config.get("user-agent", 'target-google-sheets <hello@hotglue.xyz>'))
    return credentials


def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        logger.debug('Emitting state {}'.format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()
        
def get_spreadsheet(service, spreadsheet_id):
    return service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

def get_values(service, spreadsheet_id, range):
    return service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range).execute()

def add_sheet(service, spreadsheet_id, title):
    return service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            'requests':[
                {
                    'addSheet': {
                    'properties': {
                        'title': title,
                        'gridProperties': {
                            'rowCount': 1000,
                            'columnCount': 26
                        }
                    }
                    }
                }
            ]
        }).execute()


def append_to_sheet(service, spreadsheet_id, range, values):
    return service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption='USER_ENTERED',
        body={'values': [values]}).execute()
    
def flatten(d, parent_key='', sep='__'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, str(v) if type(v) is list else v))
    return dict(items)

def persist_lines(service, spreadsheet, lines):
    state = None
    schemas = {}
    key_properties = {}

    headers_by_stream = {}
    
    for line in lines:
        try:
            msg = singer.parse_message(line)
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(line))
            raise

        if isinstance(msg, singer.RecordMessage):
            if msg.stream not in schemas:
                raise Exception("A record for stream {} was encountered before a corresponding schema".format(msg.stream))

            schema = schemas[msg.stream]
            validate(msg.record, schema)
            flattened_record = flatten(msg.record)
            
            matching_sheet = [s for s in spreadsheet['sheets'] if s['properties']['title'] == msg.stream]
            new_sheet_needed = len(matching_sheet) == 0
            range_name = "{}!A1:ZZZ".format(msg.stream)
            append = functools.partial(append_to_sheet, service, spreadsheet['spreadsheetId'], range_name)

            if new_sheet_needed:
                add_sheet(service, spreadsheet['spreadsheetId'], msg.stream)
                spreadsheet = get_spreadsheet(service, spreadsheet['spreadsheetId']) # refresh this for future iterations
                headers_by_stream[msg.stream] = list(flattened_record.keys())
                append(headers_by_stream[msg.stream])

            elif msg.stream not in headers_by_stream:
                first_row = get_values(service, spreadsheet['spreadsheetId'], range_name + '1')
                if 'values' in first_row:
                    headers_by_stream[msg.stream] = first_row.get('values', None)[0]
                else:
                    headers_by_stream[msg.stream] = list(flattened_record.keys())
                    append(headers_by_stream[msg.stream])

            result = append([flattened_record.get(x, None) for x in headers_by_stream[msg.stream]]) # order by actual headers found in sheet

            state = None
        elif isinstance(msg, singer.StateMessage):
            logger.debug('Setting state to {}'.format(msg.value))
            state = msg.value
        elif isinstance(msg, singer.SchemaMessage):
            schemas[msg.stream] = msg.schema
            key_properties[msg.stream] = msg.key_properties
        else:
            raise Exception("Unrecognized message {}".format(msg))

    return state

        
def main():
    # Read the config
    with open(flags.config) as input:
        config = json.load(input)

    # Get the Google OAuth creds
    credentials = get_credentials(config)
    http = credentials.authorize(httplib2.Http())
    discoveryUrl = ('https://sheets.googleapis.com/$discovery/rest?'
                    'version=v4')
    service = discovery.build('sheets', 'v4', http=http,
                              discoveryServiceUrl=discoveryUrl)

    # Get spreadsheet_id
    spreadsheet = get_spreadsheet(service, config['spreadsheet_id'])

    input = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    state = None
    state = persist_lines(service, spreadsheet, input)
    emit_state(state)
    logger.debug("Exiting normally")


if __name__ == '__main__':
    main()
