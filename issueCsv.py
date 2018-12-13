#!/usr/bin/env python3
#
# Copyright 2017-2018 Government of Canada
# Public Services and Procurement Canada - buyandsell.gc.ca
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import asyncio
import argparse
import csv
import json
import os
import sys
import time

import aiohttp

DEFAULT_AGENT_URL = os.environ.get('AGENT_URL', 'http://localhost:5000/sri-reg')

parser = argparse.ArgumentParser(description='Issue one or more credentials via von-x')
parser.add_argument('csv', help='the path to the csv file')
parser.add_argument('-b', '--batch', type=int, default=0, help='credential request batch size')
parser.add_argument('-p', '--parallel', action='store_true',
    help='submit the credentials in parallel')
parser.add_argument('-u', '--url', default=DEFAULT_AGENT_URL, help='the URL of the von-x service')

args = parser.parse_args()

AGENT_URL = args.url
PARALLEL = args.parallel
LIMITER = asyncio.Semaphore(value=10)
SERVED_BY = {}

async def limiter(coro):
    async with LIMITER:
        return await coro

async def issue_cred_batch(http_client, creds, ident):
    issue = []
    for cred in creds:
        schema = cred.get('schema')
        if not schema:
            raise ValueError('No schema defined')
        version = cred.get('version', None)
        attrs = cred.get('attributes')
        if not attrs:
            raise ValueError('No schema attributes defined')
        issue.append({'schema': schema, 'version': version, 'attributes': attrs})

    print('Submitting credential batch {}'.format(ident))

    start = time.time()
    try:
        response = await http_client.post(
            '{}/issue-credential'.format(AGENT_URL),
            json=issue
        )
        if response.status != 200:
            raise RuntimeError(
                'Credential batch could not be processed: {}'.format(await response.text())
            )
        result_json = await response.json()
        print(json.dumps(result_json, indent=2))
        if result_json:
            for row in result_json:
                served_by = row.get("served_by")
                if served_by:
                    SERVED_BY[served_by] = SERVED_BY.get(served_by, 0) + 1
    except Exception as exc:
        raise Exception(
            'Could not issue credential batch. '
            'Are von-x and TheOrgBook running?') from exc

    elapsed = time.time() - start
    print('Response to {} from von-x ({:.2f}s):\n\n{}\n'.format(ident, elapsed, result_json))

async def issue_cred(http_client, cred, ident):
    schema = cred.get('schema')
    if not schema:
        raise ValueError('No schema defined')
    version = cred.get('version', '')
    attrs = cred.get('attributes')
    if not attrs:
        raise ValueError('No schema attributes defined')

    print('Submitting credential {}'.format(ident))

    start = time.time()
    try:
        response = await http_client.post(
            '{}/issue-credential'.format(AGENT_URL),
            params={'schema': schema, 'version': version},
            json=attrs
        )
        if response.status != 200:
            raise RuntimeError(
                'Credential could not be processed: {}'.format(await response.text())
            )
        result_json = await response.json()
        if result_json and result_json.get("served_by"):
            served_by = result_json["served_by"]
            SERVED_BY[served_by] = SERVED_BY.get(served_by, 0) + 1
    except Exception as exc:
        raise Exception(
            'Could not issue credential. '
            'Are von-x and TheOrgBook running?') from exc

    elapsed = time.time() - start
    print('Response to {} from von-x ({:.2f}s):\n\n{}\n'.format(ident, elapsed, result_json))

CRED = {
  "schema": "pspc-sri.gc-vendor-credential",
  "attributes": {
    "effective_date": "2019-01-01T12:00:00Z",
  }
}
FIELD_MAP = {
  'legal_entity_id': 'PROCUREMENT_BUSINESS_NUMBER',
  'sri_record_id': 'PROCUREMENT_BUSINESS_NUMBER',
  'legal_name': 'AS_TYPED_LEGAL_NAME',
  'status': 'STATUS_TYPE_CODE',
  'org_type': 'TYPE_OF_OWNERSHIP_CODE',
  #'addressee'
  'address_line_1': 'ADDRESS_LINE_1',
  'address_line_2': 'ADDRESS_LINE_2',
  'city': 'CITY',
  'province': 'PROVINCE_STATE_NAME_EN',
  'postal_code': 'POSTAL_CODE',
  'country': 'COUNTRY_CODE',
  #'end_date':
}
STATUS_MAP = {
  '0': 'active',
  '1': 'deleted', # Deleted by administrator
  '2': 'incomplete',
  '3': 'suspended', # account expired
  '4': 'disbarred', # closed by CCRA
  '5': 'unavailable', # pending update
  '6': 'reassigned', # cross-referenced to a new PBN
}
ORG_TYPE_MAP = {
	'1': 'individual',
	'2': 'partnership',
	'3': 'corporation',
	'4': 'other',
}

def csv_creds(path, start=None, max=None):
  with open(path, newline='') as csvfile:
    reader = csv.reader(csvfile)
    args = next(reader)
    arg_map = {}
    for idx, a in enumerate(args):
      if a: arg_map[a] = idx
    c = 0
    for idx, row in enumerate(reader):
      if start and start > idx: continue
      c += 1
      if max and max < c:
        break
      cred = CRED.copy()
      cred["attributes"] = cred["attributes"].copy()
      for k, v in FIELD_MAP.items():
        cred["attributes"][k] = row[arg_map[v]]
      cred["attributes"]["status"] = STATUS_MAP.get(cred["attributes"]["status"])
      cred["attributes"]["org_type"] = ORG_TYPE_MAP.get(cred["attributes"]["org_type"])
      yield cred

async def submit_csv(source, batch_size=None):
    start = time.time()
    pending = 0
    last = None
    async def send_one(http_client, cred):
        nonlocal pending
        try:
            await issue_cred(http_client, cred, cred["attributes"]["legal_entity_id"])
        finally:
            pending -= 1
    async with aiohttp.ClientSession() as http_client:
        if batch_size:
            queue = []
            while True:
                batch = []
                while len(batch) < batch_size:
                    try:
                        cred = next(source)
                    except StopIteration:
                        break
                    last = cred
                    batch.append(cred)
                if not batch:
                    break
                queue.append(asyncio.ensure_future(
                    issue_cred_batch(
                        http_client, batch, batch[0]["attributes"]["legal_entity_id"]
                    )
                ))
            await asyncio.wait(queue)
        else:
            while True:
                while pending < 10:
                    try:
                        cred = next(source)
                    except StopIteration:
                        break
                    pending += 1
                    last = cred
                    asyncio.ensure_future(send_one(http_client, cred))
                if not pending:
                    break
                await asyncio.sleep(0.01)
    print(last)
    elapsed = time.time() - start
    print('Total time: {:.2f}s'.format(elapsed))
    if SERVED_BY:
        print('Served by: {}'.format(SERVED_BY))

src = csv_creds(args.csv, 100, 100)
asyncio.get_event_loop().run_until_complete(submit_csv(src, args.batch))
