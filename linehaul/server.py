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

import itertools
import uuid

from functools import partial
from typing import Optional

import arrow
import cattr
import tenacity
import trio

from linehaul.events import parser as _event_parser
from linehaul.protocol import LineReceiver
from linehaul.syslog import parser as _syslog_parser


retry = partial(tenacity.retry, sleep=trio.sleep)


_cattr = cattr.Converter()
_cattr.register_unstructure_hook(arrow.Arrow, lambda o: o.float_timestamp)


#
# Non I/O Functions
#


def parse_line(line: bytes, token=None) -> Optional[_event_parser.Download]:
    line = line.decode("utf8")

    # Check our token, and remove it from the start of the line if it matches.
    if token is not None:
        # TODO: Use a Constant Time Compare?
        if not line.startswith(token):
            return
        line = line[len(token) :]

    # Parse the incoming Syslog Message, and get the download event out of it.
    try:
        msg = _syslog_parser.parse(line)
        event = _event_parser.parse(msg.message)
    except ValueError:
        # TODO: Better Error Logging.
        return

    return event


def extract_item_date(item):
    return item.timestamp.format("YYYYMDDD")


def compute_batches(all_items):
    for date, items in itertools.groupby(
        sorted(all_items, key=extract_item_date), extract_item_date
    ):
        items = list(items)

        yield extract_item_date(items[0]), [
            {"insertId": str(uuid.uuid4()), "json": row}
            for row in _cattr.unstructure(items)
        ],


#
# I/O Functions
#


async def handle_connection(stream, q, token=None):
    lr = LineReceiver(partial(parse_line, token=token))

    while True:
        try:
            data: bytes = await stream.receive_some(1024)
        except trio.BrokenStreamError:
            data = b""

        if not data:
            lr.close()
            break

        for msg in lr.recieve_data(data):
            await q.put(msg)


@retry(
    retry=tenacity.retry_if_exception_type(trio.TooSlowError),
    wait=tenacity.wait_random_exponential(multiplier=1, max=60),
    stop=tenacity.stop_after_attempt(15),
    reraise=True,
)
async def actually_send_batch(bq, table, template_suffix, batch, api_timeout=None):
    if api_timeout is None:
        api_timeout = 15

    with trio.fail_after(api_timeout):
        await bq.insert_all(table, batch, template_suffix)


async def send_batch(*args, **kwargs):
    # We split up send_batch and actually_send_batch so that we can use tenacity to
    # handle retries for us, while still getting to use the Nurser.start_soon interface.
    # This also makes it easier to deal with the error handling aspects of sending a
    # batch, from the work of actually sending. The general rule here is that errors
    # shoudl not escape from this function.
    try:
        await actually_send_batch(*args, **kwargs)
    except Exception:
        # We've tried to send this batch to BigQuery, however for one reason or another
        # we were unable to do so. We should log this error, but otherwise we're going
        # to just drop this on the floor because there's not much else we can do here
        # except buffer it forever (which is not a great idea).
        # TODO: Add Logging
        pass


async def sender(
    bq, table, q, *, batch_size=None, batch_timeout=None, api_timeout=None
):
    if batch_size is None:
        batch_size = 3  # TODO: Change to 500
    if batch_timeout is None:
        batch_timeout = 30

    async with trio.open_nursery() as nursery:
        while True:
            batch = []
            with trio.move_on_after(batch_timeout):
                while len(batch) < batch_size:
                    batch.append(await q.get())

            for template_suffix, batch in compute_batches(batch):
                nursery.start_soon(
                    partial(
                        send_batch,
                        bq,
                        table,
                        template_suffix,
                        batch,
                        api_timeout=api_timeout,
                    )
                )


#
# Main Entry point
#


async def server(
    bq,
    table,
    bind="0.0.0.0",
    port=512,
    token=None,
    qsize=10000,
    batch_size=None,
    batch_timeout=None,
    api_timeout=None,
    task_status=trio.TASK_STATUS_IGNORED,
):
    # Total number of buffered events is:
    #       qsize + (COUNT(send_batch) * batch_size)
    # However, the length of time a single send_batch call sticks around for is time
    # boxed, so this won't grow forever. It will not however, apply any backpressure
    # to the sender (we can't meaningfully apply backpressure, since these are download
    # events being streamed to us).
    q = trio.Queue(qsize)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(
            partial(
                sender,
                bq,
                table,
                q,
                batch_size=batch_size,
                batch_timeout=batch_timeout,
                api_timeout=api_timeout,
            )
        )

        await nursery.start(
            trio.serve_tcp, partial(handle_connection, q=q, token=token), port
        )

        task_status.started()