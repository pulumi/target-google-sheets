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
import backoff
import requests
import time
import math

from jsonschema import validate
import singer

import httplib2

from apiclient import discovery
from googleapiclient.errors import HttpError
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

if sys.version_info.major == 3 and sys.version_info.minor >= 10:
    from collections.abc import MutableMapping
else:
    from collections import MutableMapping


# Read the config
try:
    parser = argparse.ArgumentParser(parents=[tools.argparser])
    parser.add_argument('-c', '--config', help='Config file', required=True)
    flags = parser.parse_args()
except ImportError:
    flags = None

logging.getLogger('backoff').setLevel(logging.CRITICAL)
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
logger = singer.get_logger()

MAX_RETRIES = 10

def get_credentials(config):
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.

    Returns:
        Credentials, the obtained credential.
    """
    auth_url = config.get('auth_url')
    if not auth_url:
        auth_url = 'https://oauth2.googleapis.com/token'

    request_time = time.time()
    oauth_request_body = {
        "grant_type": "refresh_token",
        "client_id": config.get("client_id"),
        "client_secret": config.get("client_secret"),
        "refresh_token": config.get("refresh_token"),
    }

    token_response = requests.post(
        auth_url,
        headers={},
        data=json.dumps(oauth_request_body),
    )

    try:
        token_response.raise_for_status()
        logger.info("OAuth authorization attempt was successful.")
    except Exception as ex:
        raise RuntimeError(
            f"Failed OAuth login, response was '{token_response.json()}'. {ex}"
        )
    token_json = token_response.json()
    access_token = token_json["access_token"]
    expires_in = token_json["expires_in"]

    credentials = client.OAuth2Credentials(access_token, config['client_id'], config['client_secret'], config['refresh_token'], expires_in, auth_url, config.get("user-agent", 'target-google-sheets'))
    return credentials


def divide(l, n):
    for i in range(0, len(l), n): 
        yield l[i:i + n]

def giveup(exc):
    return exc.resp is not None \
        and 400 <= int(exc.resp["status"]) < 500 \
        and int(exc.resp["status"]) != 429


def retry_handler(details):
    logger.info("Http unsuccessful request -- Retry %s/%s", details['tries'], MAX_RETRIES)


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



def append_new_lines_to_sheet(service, spreadsheet_id, sheet_id, length):
    batch_data = {
        "requests": [
            {
                "appendDimension": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "length": length
                }
            }]
    }
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=batch_data,
        ).execute()
    except HttpError as e:
        raise Exception("An error occurred while appending more grid space: {}".format(str(e)))

@backoff.on_exception(backoff.expo,
                      HttpError,
                      max_tries=MAX_RETRIES,
                      jitter=None,
                      giveup=giveup,
                      on_backoff=retry_handler)
def append_to_sheet(service, spreadsheet_id, range, values):
    try:
        return service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption='USER_ENTERED',
            body={'values': values}).execute()
    except Exception as e:
        logger.error("Error: {}".format(str(e)))

def update_to_sheet(service, spreadsheet_id, range, values):
    return service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption='USER_ENTERED',
        body={'values': [values]}).execute()


def flatten(d, parent_key='', sep='__'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, str(v) if type(v) is list else v))
    return dict(items)

def get_pk_index(properties_arr, key_properties):
    pk_indexes = []
    for i, property in enumerate(properties_arr):
        if property in key_properties:
            pk_indexes.append(i)
    return pk_indexes

def persist_lines(service, spreadsheet, lines):
    state = None
    schemas = {}
    key_properties = {}

    headers_by_stream = {}
    batch_updates = []

    for line_idx, line in enumerate(lines):
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
            range_name = "{}!A{}:ZZZ".format(msg.stream, line_idx)
            update_row = functools.partial(update_to_sheet, service, spreadsheet['spreadsheetId'])

            if new_sheet_needed:
                add_sheet(service, spreadsheet['spreadsheetId'], msg.stream)
                spreadsheet = get_spreadsheet(service, spreadsheet['spreadsheetId']) # refresh this for future iterations
                headers_by_stream[msg.stream] = list(flattened_record.keys())

            elif msg.stream not in headers_by_stream:
                first_row = get_values(service, spreadsheet['spreadsheetId'], range_name + '1')
                if 'values' in first_row:
                    headers_by_stream[msg.stream] = first_row.get('values', None)[0]
                    new_records_columns = flattened_record.keys()
                    new_columns = [col for col in new_records_columns if col not in headers_by_stream[msg.stream]]
                    if new_columns:
                        #update headers in google sheets mantaining the order of existing columns
                        new_headers = headers_by_stream[msg.stream] + new_columns
                        headers_range = "{}!A1:ZZZ1".format(msg.stream)  
                        update_row(headers_range, new_headers)
                        headers_by_stream[msg.stream] = new_headers
                        # update the primary key index for duplicates logic
                        pks = key_properties[msg.stream]
                        pk_indexes = get_pk_index(new_headers, pks)
                        key_properties[msg.stream + "_pk_index"] = pk_indexes         
                else:
                    headers_by_stream[msg.stream] = list(flattened_record.keys())
            

            sheet_id = [
                sheet['properties']["sheetId"]
                for sheet in spreadsheet['sheets'] if sheet['properties']["title"] == msg.stream
            ][0]
            sheet_row_count = [
                sheet['properties']["gridProperties"]["rowCount"]
                for sheet in spreadsheet['sheets'] if sheet['properties']["title"] == msg.stream
            ][0]

            values = [flattened_record.get(x, None) for x in headers_by_stream[msg.stream]]
            if any(len(str(value)) > 50000 for value in values):
                # Split cell values that exceed the maximum limit
                split_values = []
                for value in values:
                    if len(str(value)) > 50000:
                        chunks = [str(value)[i:i + 50000] for i in range(0, len(str(value)), 50000)]
                        split_values.extend(chunks)
                    else:
                        split_values.append(str(value))
                batch_updates.extend({
                    'range': range_name,
                    'values': [[split_value] for split_value in split_values]
                })
            else:
                batch_updates.append({
                    'range': range_name,
                    'values': [values]
                })
            
            state = None
        elif isinstance(msg, singer.StateMessage):
            logger.debug('Setting state to {}'.format(msg.value))
            state = msg.value
        elif isinstance(msg, singer.SchemaMessage):
            schemas[msg.stream] = msg.schema
            key_properties[msg.stream] = msg.key_properties

        else:
            raise Exception("Unrecognized message {}".format(msg))

    # Perform batch updates
    for idx, small_batch in enumerate(divide(batch_updates, 500)):
        logger.info(f"Iterating through smallbatch #{idx}")
        if sheet_row_count <= ((idx+1) * 500) + 1:
            logger.info(
                "Inserting more grid space ({} ROWS) for sheet {}".format(
                    500,
                    sheet_id
            ))
            append_new_lines_to_sheet(service, spreadsheet['spreadsheetId'], sheet_id, 500)

        insert_batch_data = {
            'valueInputOption': 'USER_ENTERED',
            'data': small_batch
        }
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet['spreadsheetId'],
                body=insert_batch_data,
            ).execute()
        except HttpError as e:
            raise Exception("An error occurred while appending more rows: {}".format(str(e)))

    return state


def bulk_update(service, spreadsheet_id, data):
    return service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=data).execute()
        
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
