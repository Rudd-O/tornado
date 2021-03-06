#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Implementation of an S3-like storage server based on local files.

Useful to test features that will eventually run on S3, or if you want to
run something locally that was once running on S3.

We don't support all the features of S3, but it does work with the
standard S3 client for the most basic semantics. To use the standard
S3 client with this module:

    from boto.s3.connection import S3Connection
    c = S3Connection(
                   "", "", host="localhost", port=8888,
                   is_secure=False,
                   calling_format=boto.s3.connection.OrdinaryCallingFormat()
    )

We do support multipart uploads.  Multipart uploads are buffered until the
next writable part is fully uploaded, so if you upload many parts with high
parallelism, expect increased memory usage.

"""

import bisect
import datetime
import hashlib
import os
import os.path
import urllib
import uuid
from threading import Lock
import sys

from tornado import escape
from tornado import httpserver
from tornado import ioloop
from tornado import web
from tornado.util import bytes_type

def start(port, root_directory="/tmp/s3", bucket_depth=0):
    """Starts the mock S3 server on the given port at the given path."""
    application = S3Application(root_directory, bucket_depth)
    http_server = httpserver.HTTPServer(application)
    http_server.listen(port)
    ioloop.IOLoop.instance().start()

class S3Application(web.Application):
    """Implementation of an S3-like storage server based on local files.

    If bucket depth is given, we break files up into multiple directories
    to prevent hitting file system limits for number of files in each
    directories. 1 means one level of directories, 2 means 2, etc.
    """
    def __init__(self, root_directory, bucket_depth=0):
        web.Application.__init__(self, [
            (r"/", RootHandler),
            (r"/([^/]+)/(.+)", ObjectHandler),
            (r"/([^/]+)/", BucketHandler),
        ])
        self.directory = os.path.abspath(root_directory)
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)
        self.bucket_depth = bucket_depth


class BaseRequestHandler(web.RequestHandler):
    SUPPORTED_METHODS = ("PUT", "GET", "DELETE")

    def render_xml(self, value):
        assert isinstance(value, dict) and len(value) == 1
        self.set_header("Content-Type", "application/xml; charset=UTF-8")
        name = value.keys()[0]
        parts = []
        parts.append('<' + escape.utf8(name) +
                     ' xmlns="http://doc.s3.amazonaws.com/2006-03-01">')
        self._render_parts(value.values()[0], parts)
        parts.append('</' + escape.utf8(name) + '>')
        self.finish('<?xml version="1.0" encoding="UTF-8"?>\n' +
                    ''.join(parts))

    def _render_parts(self, value, parts=[]):
        if isinstance(value, (unicode, bytes_type)):
            parts.append(escape.xhtml_escape(value))
        elif isinstance(value, int) or isinstance(value, long):
            parts.append(str(value))
        elif isinstance(value, datetime.datetime):
            parts.append(value.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        elif isinstance(value, dict):
            for name, subvalue in value.iteritems():
                if not isinstance(subvalue, list):
                    subvalue = [subvalue]
                for subsubvalue in subvalue:
                    parts.append('<' + escape.utf8(name) + '>')
                    self._render_parts(subsubvalue, parts)
                    parts.append('</' + escape.utf8(name) + '>')
        else:
            raise Exception("Unknown S3 value type %r", value)

    def _object_path(self, bucket, object_name):
        if self.application.bucket_depth < 1:
            return os.path.abspath(os.path.join(
                self.application.directory, bucket, object_name))
        hash = hashlib.md5(object_name).hexdigest()
        path = os.path.abspath(os.path.join(
            self.application.directory, bucket))
        for i in range(self.application.bucket_depth):
            path = os.path.join(path, hash[:2 * (i + 1)])
        return os.path.join(path, object_name)


class RootHandler(BaseRequestHandler):
    def get(self):
        names = os.listdir(self.application.directory)
        buckets = []
        for name in names:
            path = os.path.join(self.application.directory, name)
            info = os.stat(path)
            buckets.append({
                "Name": name,
                "CreationDate": datetime.datetime.utcfromtimestamp(
                    info.st_ctime),
            })
        self.render_xml({"ListAllMyBucketsResult": {
            "Buckets": {"Bucket": buckets},
        }})


class BucketHandler(BaseRequestHandler):
    def get(self, bucket_name):
        prefix = self.get_argument("prefix", u"")
        marker = self.get_argument("marker", u"")
        max_keys = int(self.get_argument("max-keys", 50000))
        path = os.path.abspath(os.path.join(self.application.directory,
                                            bucket_name))
        terse = int(self.get_argument("terse", 0))
        if not path.startswith(self.application.directory) or \
           not os.path.isdir(path):
            raise web.HTTPError(404)
        object_names = []
        for root, dirs, files in os.walk(path):
            for file_name in files:
                object_names.append(os.path.join(root, file_name))
        skip = len(path) + 1
        for i in range(self.application.bucket_depth):
            skip += 2 * (i + 1) + 1
        object_names = [n[skip:] for n in object_names]
        object_names.sort()
        contents = []

        start_pos = 0
        if marker:
            start_pos = bisect.bisect_right(object_names, marker, start_pos)
        if prefix:
            start_pos = bisect.bisect_left(object_names, prefix, start_pos)

        truncated = False
        for object_name in object_names[start_pos:]:
            if not object_name.startswith(prefix):
                break
            if len(contents) >= max_keys:
                truncated = True
                break
            object_path = self._object_path(bucket_name, object_name)
            c = {"Key": object_name}
            if not terse:
                info = os.stat(object_path)
                c.update({
                    "LastModified": datetime.datetime.utcfromtimestamp(
                        info.st_mtime),
                    "Size": info.st_size,
                })
            contents.append(c)
            marker = object_name
        self.render_xml({"ListBucketResult": {
            "Name": bucket_name,
            "Prefix": prefix,
            "Marker": marker,
            "MaxKeys": max_keys,
            "IsTruncated": truncated,
            "Contents": contents,
        }})

    def put(self, bucket_name):
        path = os.path.abspath(os.path.join(
            self.application.directory, bucket_name))
        if not path.startswith(self.application.directory) or \
           os.path.exists(path):
            raise web.HTTPError(403)
        os.makedirs(path)
        self.finish()

    def delete(self, bucket_name):
        path = os.path.abspath(os.path.join(
            self.application.directory, bucket_name))
        if not path.startswith(self.application.directory) or \
           not os.path.isdir(path):
            raise web.HTTPError(404)
        if len(os.listdir(path)) > 0:
            raise web.HTTPError(403)
        os.rmdir(path)
        self.set_status(204)
        self.finish()


class ObjectHandler(BaseRequestHandler):
    SUPPORTED_METHODS = ("PUT", "GET", "DELETE", "POST")

    def get(self, bucket, object_name):
        object_name = urllib.unquote(object_name)
        path = self._object_path(bucket, object_name)
        if not path.startswith(self.application.directory) or \
           not os.path.isfile(path):
            raise web.HTTPError(404)
        info = os.stat(path)
        self.set_header("Content-Type", "application/unknown")
        self.set_header("Last-Modified", datetime.datetime.utcfromtimestamp(
            info.st_mtime))
        object_file = open(path, "rb")
        try:
            chunk = object_file.read(1048576)
            while chunk:
                self.write(chunk)
                self.flush()
                chunk = object_file.read(1048576)
            self.finish()
        finally:
            object_file.close()

    def put(self, bucket, object_name):
        object_name = urllib.unquote(object_name)
        bucket_dir = os.path.abspath(os.path.join(
            self.application.directory, bucket))
        if not bucket_dir.startswith(self.application.directory) or \
           not os.path.isdir(bucket_dir):
            raise web.HTTPError(404)
        path = self._object_path(bucket, object_name)
        if not path.startswith(bucket_dir) or os.path.isdir(path):
            raise web.HTTPError(403)
        directory = os.path.dirname(path)
        if not os.path.exists(directory):
            os.makedirs(directory)
        part_number = self._get_part_number()
        upload_id = self._get_upload_id()
        if part_number and upload_id:
            part_number = int(part_number)
            object_file = MultipartUploaderFactory(upload_id, None)
            if not object_file:
                raise web.HTTPError(404, "No such upload ID %r" % upload_id)
            object_file.write(part_number, self.request.body)
            self.set_header("ETag", object_file.buffers[part_number].md5sum)
        else:
            object_file = open(path, "w")
            object_file.write(self.request.body)
            object_file.close()
            self.set_header("ETag", "ignored")
        # there is a bug in line 178 of s3/s3util/uploader.go
        # it chokes if no ETag is returned to it
        # p.ETag = s[1 : len(s)-1]
        # so I add this nonsense as a compromise
        self.finish()

    def _get_upload_id(self):
        upload_id = self.get_argument("UploadId", None)
        if not upload_id:
            upload_id = self.get_argument("uploadId", None)
        return upload_id

    def _get_part_number(self):
        upload_id = self.get_argument("PartNumber", None)
        if not upload_id:
            upload_id = self.get_argument("partNumber", None)
        return upload_id

    def delete(self, bucket, object_name):
        upload_id = self._get_upload_id()
        if upload_id:
            object_file = MultipartUploaderFactory(upload_id, None)
            if not object_file:
                raise web.HTTPError(
                    404, "No such upload ID %r" % upload_id
            )
            object_file.abort()
            self.set_status(204)
            self.finish()
            return
        object_name = urllib.unquote(object_name)
        path = self._object_path(bucket, object_name)
        if not path.startswith(self.application.directory) or \
           not os.path.isfile(path):
            raise web.HTTPError(404)
        os.unlink(path)
        self.set_status(204)
        self.finish()

    def post(self, bucket, object_name):
        '''Implements multipart uploads in a hacky way'''
        upload_id = self._get_upload_id()
        if upload_id:
            object_file = MultipartUploaderFactory(upload_id, None)
            if not object_file:
                raise web.HTTPError(
                    404, "No such upload ID %r" % upload_id
            )
            object_file.close()
            self.set_status(200)
            complete_multipart_upload = "\n".join([
                '''  <Part><PartNumber>%s</Bucket>
  <ETag>%s</ETag></Part>''' % (pn, object_file.buffers[pn].md5sum)
                for pn in range(1, object_file.part_number)
            ])
            xml = '''<?xml version="1.0" encoding="UTF-8"?>
<CompleteMultipartUpload xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
%s
</CompleteMultipartUpload>''' % complete_multipart_upload
            self.finish(xml)
            return
        else:
            if not self.request.uri.endswith("?uploads"):
                if not self.request.uri.endswith("?uploads="):
                    raise web.HTTPError(
                        400, "No ?uploads parameter specified"
                    )
            bucket_dir = os.path.abspath(os.path.join(
                self.application.directory, bucket))
            if not bucket_dir.startswith(self.application.directory) or \
               not os.path.isdir(bucket_dir):
                raise web.HTTPError(404)
            path = self._object_path(bucket, object_name)
            if not path.startswith(bucket_dir) or os.path.isdir(path):
                raise web.HTTPError(403)
            directory = os.path.dirname(path)
            if not os.path.exists(directory):
                os.makedirs(directory)
            gen_id = uuid.uuid4().hex
            m = MultipartUploaderFactory(gen_id, path)
            self.set_status(200)
            xml = '''<?xml version="1.0" encoding="UTF-8"?>
<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Bucket>%s</Bucket>
  <Key>%s</Key>
  <UploadId>%s</UploadId>
</InitiateMultipartUploadResult>''' % (bucket, object_name, m.upload_id)
            self.finish(xml)


multipart_uploaders = {}
multipart_uploaders_lock = Lock()

class _MultipartBuffer(object):
    def __init__(self, data):
        self.data = data
        self.md5sum = hashlib.md5(data).hexdigest()

    def forget_data(self):
        self.data = None

class _MultipartUploader(object):

    path = None
    upload_id = None
    file = None
    lock = None
    part_number = None

    def __init__(self, upload_id, path):
        self.path = path
        self.upload_id = upload_id
        self.file = open(path, "w")
        self.lock = Lock()
        self.part_number = 1
        self.buffers = {}

    def write(self, part_number, data):
        with self.lock:
            if part_number in self.buffers:
                print "Buffer received:", len(data)
                raise web.HTTPError(
                    400, "PartNumber %r already received" % part_number
                )
            self.buffers[part_number] = _MultipartBuffer(data)
        self.trigger_write_completion()

    def trigger_write_completion(self):
        with self.lock:
            while self.part_number in self.buffers:
                self.file.write(self.buffers[self.part_number].data)
                self.buffers[self.part_number].forget_data()
                self.part_number = self.part_number + 1

    def close(self):
        global multipart_uploaders
        global multipart_uploaders_lock

        self.trigger_write_completion()
        self.file.flush()
        self.file.close()
        with multipart_uploaders_lock:
            del multipart_uploaders[self.upload_id]

    def abort(self):
        global multipart_uploaders
        global multipart_uploaders_lock

        self.file.close()
        os.unlink(self.path)
        with multipart_uploaders_lock:
            del multipart_uploaders[self.upload_id]


def MultipartUploaderFactory(upload_id, path):
    '''Returns a multipart uploader, whether an existing one for a
    file that is being received in parts, or a new one for a file
    that is about to be received in parts'''
    global multipart_uploaders
    global multipart_uploaders_lock

    with multipart_uploaders_lock:
        if upload_id in multipart_uploaders:
            return multipart_uploaders[upload_id]
        if not path:
            return None  # no such upload ID
        m = _MultipartUploader(upload_id, path)
        multipart_uploaders[upload_id] = m
        return m


def main():
    try:
        start(int(sys.argv[1]), sys.argv[2])
    except IndexError:
        print >> sys.stderr, "usage: %s <integer port> <S3 store directory>" % sys.argv[0]

if __name__ == "__main__":
    main()
