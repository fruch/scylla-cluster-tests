# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2020 ScyllaDB

import os
import logging
import tempfile
import contextlib
from io import StringIO

import yaml

from sdcm import wait


LOGGER = logging.getLogger(__name__)


def read_to_stringio(fobj):
    return StringIO(fobj.read())


# pylint: disable=too-many-locals,too-many-arguments
@contextlib.contextmanager
def remote_file(remoter, remote_path, serializer=StringIO.getvalue, deserializer=read_to_stringio, sudo=False,
                preserve_ownership=True, preserve_permissions=True):
    filename = os.path.basename(remote_path)
    local_tempfile = os.path.join(tempfile.mkdtemp(prefix='sct'), filename)
    if preserve_ownership:
        ownership = remoter.sudo(cmd='stat -c "%U:%G" ' + remote_path).stdout.strip()
    if preserve_permissions:
        permissions = remoter.sudo(cmd='stat -c "%a" ' + remote_path).stdout.strip()

    wait.wait_for(remoter.receive_files,
                  step=10,
                  text=f"Waiting for copying `{remote_path}' from {remoter.hostname}",
                  timeout=300,
                  throw_exc=True,
                  src=remote_path,
                  dst=local_tempfile)
    with open(local_tempfile, encoding="utf-8") as fobj:
        parsed_data = deserializer(fobj)
        original_content = serializer(parsed_data)
    yield parsed_data

    content = serializer(parsed_data)

    if original_content == content:
        LOGGER.debug("Content of '%s' wasn't changed", remote_path)
    else:
        with open(local_tempfile, "w", encoding="utf-8") as fobj:
            fobj.write(content)

        LOGGER.debug("New content of `%s':\n%s", remote_path, content)

        remote_tempfile = remoter.run("mktemp").stdout.strip()
        remote_tempfile_move_cmd = f"mv '{remote_tempfile}' '{remote_path}'"
        wait.wait_for(remoter.send_files,
                      step=10,
                      text=f"Waiting for updating of `{remote_path}' on {remoter.hostname}",
                      timeout=300,
                      throw_exc=True,
                      src=local_tempfile,
                      dst=remote_tempfile)
        if sudo:
            remoter.sudo(remote_tempfile_move_cmd)
        else:
            remoter.run(remote_tempfile_move_cmd)

        if preserve_ownership:
            remoter.sudo(f"chown {ownership} {remote_path}")
        if preserve_permissions:
            remoter.sudo(f"chmod {permissions} {remote_path}")

    os.unlink(local_tempfile)


def yaml_file_to_dict(fobj):
    return yaml.safe_load(fobj) or {}


def dict_to_yaml_file(data):
    return yaml.safe_dump(data) if data else ""
