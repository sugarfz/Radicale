# This file is part of Radicale Server - Calendar Server
# Copyright © 2008 Nicolas Kandel
# Copyright © 2008 Pascal Halter
# Copyright © 2008-2017 Guillaume Ayoub
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Radicale.  If not, see <http://www.gnu.org/licenses/>.

"""
Radicale WSGI application.

Can be used with an external WSGI server or the built-in server.

"""

import base64
import contextlib
import datetime
import io
import itertools
import logging
import os
import pkg_resources
import posixpath
import pprint
import random
import socket
import sys
import threading
import time
import zlib
from http import client
from urllib.parse import urlparse, quote
from xml.etree import ElementTree as ET

import vobject

from radicale import auth, config, log, rights, storage, web, xmlutils
from radicale.log import logger

VERSION = pkg_resources.get_distribution("radicale").version

NOT_ALLOWED = (
    client.FORBIDDEN, (("Content-Type", "text/plain"),),
    "Access to the requested resource forbidden.")
FORBIDDEN = (
    client.FORBIDDEN, (("Content-Type", "text/plain"),),
    "Action on the requested resource refused.")
BAD_REQUEST = (
    client.BAD_REQUEST, (("Content-Type", "text/plain"),), "Bad Request")
NOT_FOUND = (
    client.NOT_FOUND, (("Content-Type", "text/plain"),),
    "The requested resource could not be found.")
CONFLICT = (
    client.CONFLICT, (("Content-Type", "text/plain"),),
    "Conflict in the request.")
WEBDAV_PRECONDITION_FAILED = (
    client.CONFLICT, (("Content-Type", "text/plain"),),
    "WebDAV precondition failed.")
METHOD_NOT_ALLOWED = (
    client.METHOD_NOT_ALLOWED, (("Content-Type", "text/plain"),),
    "The method is not allowed on the requested resource.")
PRECONDITION_FAILED = (
    client.PRECONDITION_FAILED,
    (("Content-Type", "text/plain"),), "Precondition failed.")
REQUEST_TIMEOUT = (
    client.REQUEST_TIMEOUT, (("Content-Type", "text/plain"),),
    "Connection timed out.")
REQUEST_ENTITY_TOO_LARGE = (
    client.REQUEST_ENTITY_TOO_LARGE, (("Content-Type", "text/plain"),),
    "Request body too large.")
REMOTE_DESTINATION = (
    client.BAD_GATEWAY, (("Content-Type", "text/plain"),),
    "Remote destination not supported.")
DIRECTORY_LISTING = (
    client.FORBIDDEN, (("Content-Type", "text/plain"),),
    "Directory listings are not supported.")
INTERNAL_SERVER_ERROR = (
    client.INTERNAL_SERVER_ERROR, (("Content-Type", "text/plain"),),
    "A server error occurred.  Please contact the administrator.")

DAV_HEADERS = "1, 2, 3, calendar-access, addressbook, extended-mkcol"


class Application:
    """WSGI application managing collections."""

    def __init__(self, configuration):
        """Initialize application."""
        super().__init__()
        self.configuration = configuration
        self.Auth = auth.load(configuration)
        self.Collection = storage.load(configuration)
        self.Rights = rights.load(configuration)
        self.Web = web.load(configuration)
        self.encoding = configuration.get("encoding", "request")

    def headers_log(self, environ):
        """Sanitize headers for logging."""
        request_environ = dict(environ)

        # Mask passwords
        mask_passwords = self.configuration.getboolean(
            "logging", "mask_passwords")
        authorization = request_environ.get("HTTP_AUTHORIZATION", "")
        if mask_passwords and authorization.startswith("Basic"):
            request_environ["HTTP_AUTHORIZATION"] = "Basic **masked**"
        if request_environ.get("HTTP_COOKIE"):
            request_environ["HTTP_COOKIE"] = "**masked**"

        return request_environ

    def decode(self, text, environ):
        """Try to magically decode ``text`` according to given ``environ``."""
        # List of charsets to try
        charsets = []

        # First append content charset given in the request
        content_type = environ.get("CONTENT_TYPE")
        if content_type and "charset=" in content_type:
            charsets.append(
                content_type.split("charset=")[1].split(";")[0].strip())
        # Then append default Radicale charset
        charsets.append(self.encoding)
        # Then append various fallbacks
        charsets.append("utf-8")
        charsets.append("iso8859-1")

        # Try to decode
        for charset in charsets:
            try:
                return text.decode(charset)
            except UnicodeDecodeError:
                pass
        raise UnicodeDecodeError

    def collect_allowed_items(self, items, user):
        """Get items from request that user is allowed to access."""
        for item in items:
            if isinstance(item, storage.BaseCollection):
                path = storage.sanitize_path("/%s/" % item.path)
                if item.get_meta("tag"):
                    permissions = self.Rights.authorized(user, path, "rw")
                    target = "collection with tag %r" % item.path
                else:
                    permissions = self.Rights.authorized(user, path, "RW")
                    target = "collection %r" % item.path
            else:
                path = storage.sanitize_path("/%s/" % item.collection.path)
                permissions = self.Rights.authorized(user, path, "rw")
                target = "item %r from %r" % (item.href, item.collection.path)
            if rights.intersect_permissions(permissions, "Ww"):
                permission = "w"
                status = "write"
            elif rights.intersect_permissions(permissions, "Rr"):
                permission = "r"
                status = "read"
            else:
                permission = ""
                status = "NO"
            logger.debug(
                "%s has %s access to %s",
                repr(user) if user else "anonymous user", status, target)
            if permission:
                yield item, permission

    def __call__(self, environ, start_response):
        with log.register_stream(environ["wsgi.errors"]):
            try:
                status, headers, answers = self._handle_request(environ)
            except Exception as e:
                try:
                    method = str(environ["REQUEST_METHOD"])
                except Exception:
                    method = "unknown"
                try:
                    path = str(environ.get("PATH_INFO", ""))
                except Exception:
                    path = ""
                logger.error("An exception occurred during %s request on %r: "
                             "%s", method, path, e, exc_info=True)
                status, headers, answer = INTERNAL_SERVER_ERROR
                answer = answer.encode("ascii")
                status = "%d %s" % (
                    status, client.responses.get(status, "Unknown"))
                headers = [
                    ("Content-Length", str(len(answer)))] + list(headers)
                answers = [answer]
            start_response(status, headers)
        return answers

    def _handle_request(self, environ):
        """Manage a request."""
        def response(status, headers=(), answer=None):
            headers = dict(headers)
            # Set content length
            if answer:
                if hasattr(answer, "encode"):
                    logger.debug("Response content:\n%s", answer)
                    headers["Content-Type"] += "; charset=%s" % self.encoding
                    answer = answer.encode(self.encoding)
                accept_encoding = [
                    encoding.strip() for encoding in
                    environ.get("HTTP_ACCEPT_ENCODING", "").split(",")
                    if encoding.strip()]

                if "gzip" in accept_encoding:
                    zcomp = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
                    answer = zcomp.compress(answer) + zcomp.flush()
                    headers["Content-Encoding"] = "gzip"

                headers["Content-Length"] = str(len(answer))

            # Add extra headers set in configuration
            if self.configuration.has_section("headers"):
                for key in self.configuration.options("headers"):
                    headers[key] = self.configuration.get("headers", key)

            # Start response
            time_end = datetime.datetime.now()
            status = "%d %s" % (
                status, client.responses.get(status, "Unknown"))
            logger.info(
                "%s response status for %r%s in %.3f seconds: %s",
                environ["REQUEST_METHOD"], environ.get("PATH_INFO", ""),
                depthinfo, (time_end - time_begin).total_seconds(), status)
            # Return response content
            return status, list(headers.items()), [answer] if answer else []

        remote_host = "unknown"
        if environ.get("REMOTE_HOST"):
            remote_host = repr(environ["REMOTE_HOST"])
        elif environ.get("REMOTE_ADDR"):
            remote_host = environ["REMOTE_ADDR"]
        if environ.get("HTTP_X_FORWARDED_FOR"):
            remote_host = "%r (forwarded by %s)" % (
                environ["HTTP_X_FORWARDED_FOR"], remote_host)
        remote_useragent = ""
        if environ.get("HTTP_USER_AGENT"):
            remote_useragent = " using %r" % environ["HTTP_USER_AGENT"]
        depthinfo = ""
        if environ.get("HTTP_DEPTH"):
            depthinfo = " with depth %r" % environ["HTTP_DEPTH"]
        time_begin = datetime.datetime.now()
        logger.info(
            "%s request for %r%s received from %s%s",
            environ["REQUEST_METHOD"], environ.get("PATH_INFO", ""), depthinfo,
            remote_host, remote_useragent)
        headers = pprint.pformat(self.headers_log(environ))
        logger.debug("Request headers:\n%s", headers)

        # Let reverse proxies overwrite SCRIPT_NAME
        if "HTTP_X_SCRIPT_NAME" in environ:
            # script_name must be removed from PATH_INFO by the client.
            unsafe_base_prefix = environ["HTTP_X_SCRIPT_NAME"]
            logger.debug("Script name overwritten by client: %r",
                         unsafe_base_prefix)
        else:
            # SCRIPT_NAME is already removed from PATH_INFO, according to the
            # WSGI specification.
            unsafe_base_prefix = environ.get("SCRIPT_NAME", "")
        # Sanitize base prefix
        base_prefix = storage.sanitize_path(unsafe_base_prefix).rstrip("/")
        logger.debug("Sanitized script name: %r", base_prefix)
        # Sanitize request URI (a WSGI server indicates with an empty path,
        # that the URL targets the application root without a trailing slash)
        path = storage.sanitize_path(environ.get("PATH_INFO", ""))
        logger.debug("Sanitized path: %r", path)

        # Get function corresponding to method
        function = getattr(self, "do_%s" % environ["REQUEST_METHOD"].upper())

        # If "/.well-known" is not available, clients query "/"
        if path == "/.well-known" or path.startswith("/.well-known/"):
            return response(*NOT_FOUND)

        # Ask authentication backend to check rights
        login = password = ""
        external_login = self.Auth.get_external_login(environ)
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if external_login:
            login, password = external_login
            login, password = login or "", password or ""
        elif authorization.startswith("Basic"):
            authorization = authorization[len("Basic"):].strip()
            login, password = self.decode(base64.b64decode(
                authorization.encode("ascii")), environ).split(":", 1)

        user = self.Auth.login(login, password) or "" if login else ""
        if user and login == user:
            logger.info("Successful login: %r", user)
        elif user:
            logger.info("Successful login: %r -> %r", login, user)
        elif login:
            logger.info("Failed login attempt: %r", login)
            # Random delay to avoid timing oracles and bruteforce attacks
            delay = self.configuration.getfloat("auth", "delay")
            if delay > 0:
                random_delay = delay * (0.5 + random.random())
                logger.debug("Sleeping %.3f seconds", random_delay)
                time.sleep(random_delay)

        if user and not storage.is_safe_path_component(user):
            # Prevent usernames like "user/calendar.ics"
            logger.info("Refused unsafe username: %r", user)
            user = ""

        # Create principal collection
        if user:
            principal_path = "/%s/" % user
            if self.Rights.authorized(user, principal_path, "W"):
                with self.Collection.acquire_lock("r", user):
                    principal = next(
                        self.Collection.discover(principal_path, depth="1"),
                        None)
                if not principal:
                    with self.Collection.acquire_lock("w", user):
                        try:
                            self.Collection.create_collection(principal_path)
                        except ValueError as e:
                            logger.warning("Failed to create principal "
                                           "collection %r: %s", user, e)
                            user = ""
            else:
                logger.warning("Access to principal path %r denied by "
                               "rights backend", principal_path)

        if self.configuration.getboolean("internal", "internal_server"):
            # Verify content length
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
            if content_length:
                max_content_length = self.configuration.getint(
                    "server", "max_content_length")
                if max_content_length and content_length > max_content_length:
                    logger.info("Request body too large: %d", content_length)
                    return response(*REQUEST_ENTITY_TOO_LARGE)

        if not login or user:
            status, headers, answer = function(
                environ, base_prefix, path, user)
            if (status, headers, answer) == NOT_ALLOWED:
                logger.info("Access to %r denied for %s", path,
                            repr(user) if user else "anonymous user")
        else:
            status, headers, answer = NOT_ALLOWED

        if ((status, headers, answer) == NOT_ALLOWED and not user and
                not external_login):
            # Unknown or unauthorized user
            logger.debug("Asking client for authentication")
            status = client.UNAUTHORIZED
            realm = self.configuration.get("auth", "realm")
            headers = dict(headers)
            headers.update({
                "WWW-Authenticate":
                "Basic realm=\"%s\"" % realm})

        return response(status, headers, answer)

    def _access(self, user, path, permission, item=None):
        if permission not in "rw":
            raise ValueError("Invalid permission argument: %r" % permission)
        if not item:
            permissions = permission + permission.upper()
            parent_permissions = permission
        elif isinstance(item, storage.BaseCollection):
            if item.get_meta("tag"):
                permissions = permission
            else:
                permissions = permission.upper()
            parent_permissions = ""
        else:
            permissions = ""
            parent_permissions = permission
        if permissions and self.Rights.authorized(user, path, permissions):
            return True
        if parent_permissions:
            parent_path = storage.sanitize_path(
                "/%s/" % posixpath.dirname(path.strip("/")))
            if self.Rights.authorized(user, parent_path, parent_permissions):
                return True
        return False

    def _read_raw_content(self, environ):
        content_length = int(environ.get("CONTENT_LENGTH") or 0)
        if not content_length:
            return b""
        content = environ["wsgi.input"].read(content_length)
        if len(content) < content_length:
            raise RuntimeError("Request body too short: %d" % len(content))
        return content

    def _read_content(self, environ):
        content = self.decode(self._read_raw_content(environ), environ)
        logger.debug("Request content:\n%s", content)
        return content

    def _read_xml_content(self, environ):
        content = self.decode(self._read_raw_content(environ), environ)
        if not content:
            return None
        try:
            xml_content = ET.fromstring(content)
        except ET.ParseError as e:
            logger.debug("Request content (Invalid XML):\n%s", content)
            raise RuntimeError("Failed to parse XML: %s" % e) from e
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Request content:\n%s",
                         xmlutils.pretty_xml(xml_content))
        return xml_content

    def _write_xml_content(self, xml_content):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Response content:\n%s",
                         xmlutils.pretty_xml(xml_content))
        f = io.BytesIO()
        ET.ElementTree(xml_content).write(f, encoding=self.encoding,
                                          xml_declaration=True)
        return f.getvalue()

    def _webdav_error_response(self, namespace, name,
                               status=WEBDAV_PRECONDITION_FAILED[0]):
        """Generate XML error response."""
        headers = {"Content-Type": "text/xml; charset=%s" % self.encoding}
        content = self._write_xml_content(
            xmlutils.webdav_error(namespace, name))
        return status, headers, content

    def _propose_filename(self, collection):
        """Propose a filename for a collection."""
        tag = collection.get_meta("tag")
        if tag == "VADDRESSBOOK":
            fallback_title = "Address book"
            suffix = ".vcf"
        elif tag == "VCALENDAR":
            fallback_title = "Calendar"
            suffix = ".ics"
        else:
            fallback_title = posixpath.basename(collection.path)
            suffix = ""
        title = collection.get_meta("D:displayname") or fallback_title
        if title and not title.lower().endswith(suffix.lower()):
            title += suffix
        return title

    def _content_disposition_attachement(self, filename):
        value = "attachement"
        try:
            encoded_filename = quote(filename, encoding=self.encoding)
        except UnicodeEncodeError as e:
            logger.warning("Failed to encode filename: %r", filename,
                           exc_info=True)
            encoded_filename = ""
        if encoded_filename:
            value += "; filename*=%s''%s" % (self.encoding, encoded_filename)
        return value

    def do_DELETE(self, environ, base_prefix, path, user):
        """Manage DELETE request."""
        if not self._access(user, path, "w"):
            return NOT_ALLOWED
        with self.Collection.acquire_lock("w", user):
            item = next(self.Collection.discover(path), None)
            if not item:
                return NOT_FOUND
            if not self._access(user, path, "w", item):
                return NOT_ALLOWED
            if_match = environ.get("HTTP_IF_MATCH", "*")
            if if_match not in ("*", item.etag):
                # ETag precondition not verified, do not delete item
                return PRECONDITION_FAILED
            if isinstance(item, storage.BaseCollection):
                xml_answer = xmlutils.delete(base_prefix, path, item)
            else:
                xml_answer = xmlutils.delete(
                    base_prefix, path, item.collection, item.href)
            headers = {"Content-Type": "text/xml; charset=%s" % self.encoding}
            return client.OK, headers, self._write_xml_content(xml_answer)

    def do_GET(self, environ, base_prefix, path, user):
        """Manage GET request."""
        # Redirect to .web if the root URL is requested
        if not path.strip("/"):
            web_path = ".web"
            if not environ.get("PATH_INFO"):
                web_path = posixpath.join(posixpath.basename(base_prefix),
                                          web_path)
            return (client.FOUND,
                    {"Location": web_path, "Content-Type": "text/plain"},
                    "Redirected to %s" % web_path)
        # Dispatch .web URL to web module
        if path == "/.web" or path.startswith("/.web/"):
            return self.Web.get(environ, base_prefix, path, user)
        if not self._access(user, path, "r"):
            return NOT_ALLOWED
        with self.Collection.acquire_lock("r", user):
            item = next(self.Collection.discover(path), None)
            if not item:
                return NOT_FOUND
            if not self._access(user, path, "r", item):
                return NOT_ALLOWED
            if isinstance(item, storage.BaseCollection):
                tag = item.get_meta("tag")
                if not tag:
                    return DIRECTORY_LISTING
                content_type = xmlutils.MIMETYPES[tag]
                content_disposition = self._content_disposition_attachement(
                    self._propose_filename(item))
            else:
                content_type = xmlutils.OBJECT_MIMETYPES[item.name]
                content_disposition = ""
            headers = {
                "Content-Type": content_type,
                "Last-Modified": item.last_modified,
                "ETag": item.etag}
            if content_disposition:
                headers["Content-Disposition"] = content_disposition
            answer = item.serialize()
            return client.OK, headers, answer

    def do_HEAD(self, environ, base_prefix, path, user):
        """Manage HEAD request."""
        status, headers, answer = self.do_GET(
            environ, base_prefix, path, user)
        return status, headers, None

    def do_MKCALENDAR(self, environ, base_prefix, path, user):
        """Manage MKCALENDAR request."""
        if not self.Rights.authorized(user, path, "w"):
            return NOT_ALLOWED
        try:
            xml_content = self._read_xml_content(environ)
        except RuntimeError as e:
            logger.warning(
                "Bad MKCALENDAR request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        except socket.timeout as e:
            logger.debug("client timed out", exc_info=True)
            return REQUEST_TIMEOUT
        # Prepare before locking
        props = xmlutils.props_from_request(xml_content)
        props["tag"] = "VCALENDAR"
        # TODO: use this?
        # timezone = props.get("C:calendar-timezone")
        try:
            storage.check_and_sanitize_props(props)
        except ValueError as e:
            logger.warning(
                "Bad MKCALENDAR request on %r: %s", path, e, exc_info=True)
        with self.Collection.acquire_lock("w", user):
            item = next(self.Collection.discover(path), None)
            if item:
                return self._webdav_error_response(
                    "D", "resource-must-be-null")
            parent_path = storage.sanitize_path(
                "/%s/" % posixpath.dirname(path.strip("/")))
            parent_item = next(self.Collection.discover(parent_path), None)
            if not parent_item:
                return CONFLICT
            if (not isinstance(parent_item, storage.BaseCollection) or
                    parent_item.get_meta("tag")):
                return FORBIDDEN
            try:
                self.Collection.create_collection(path, props=props)
            except ValueError as e:
                logger.warning(
                    "Bad MKCALENDAR request on %r: %s", path, e, exc_info=True)
                return BAD_REQUEST
            return client.CREATED, {}, None

    def do_MKCOL(self, environ, base_prefix, path, user):
        """Manage MKCOL request."""
        permissions = self.Rights.authorized(user, path, "Ww")
        if not permissions:
            return NOT_ALLOWED
        try:
            xml_content = self._read_xml_content(environ)
        except RuntimeError as e:
            logger.warning(
                "Bad MKCOL request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        except socket.timeout as e:
            logger.debug("client timed out", exc_info=True)
            return REQUEST_TIMEOUT
        # Prepare before locking
        props = xmlutils.props_from_request(xml_content)
        try:
            storage.check_and_sanitize_props(props)
        except ValueError as e:
            logger.warning(
                "Bad MKCOL request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        if (props.get("tag") and "w" not in permissions or
                not props.get("tag") and "W" not in permissions):
            return NOT_ALLOWED
        with self.Collection.acquire_lock("w", user):
            item = next(self.Collection.discover(path), None)
            if item:
                return METHOD_NOT_ALLOWED
            parent_path = storage.sanitize_path(
                "/%s/" % posixpath.dirname(path.strip("/")))
            parent_item = next(self.Collection.discover(parent_path), None)
            if not parent_item:
                return CONFLICT
            if (not isinstance(parent_item, storage.BaseCollection) or
                    parent_item.get_meta("tag")):
                return FORBIDDEN
            try:
                self.Collection.create_collection(path, props=props)
            except ValueError as e:
                logger.warning(
                    "Bad MKCOL request on %r: %s", path, e, exc_info=True)
                return BAD_REQUEST
            return client.CREATED, {}, None

    def do_MOVE(self, environ, base_prefix, path, user):
        """Manage MOVE request."""
        raw_dest = environ.get("HTTP_DESTINATION", "")
        to_url = urlparse(raw_dest)
        if to_url.netloc != environ["HTTP_HOST"]:
            logger.info("Unsupported destination address: %r", raw_dest)
            # Remote destination server, not supported
            return REMOTE_DESTINATION
        if not self._access(user, path, "w"):
            return NOT_ALLOWED
        to_path = storage.sanitize_path(to_url.path)
        if not (to_path + "/").startswith(base_prefix + "/"):
            logger.warning("Destination %r from MOVE request on %r doesn't "
                           "start with base prefix", to_path, path)
            return NOT_ALLOWED
        to_path = to_path[len(base_prefix):]
        if not self._access(user, to_path, "w"):
            return NOT_ALLOWED

        with self.Collection.acquire_lock("w", user):
            item = next(self.Collection.discover(path), None)
            if not item:
                return NOT_FOUND
            if (not self._access(user, path, "w", item) or
                    not self._access(user, to_path, "w", item)):
                return NOT_ALLOWED
            if isinstance(item, storage.BaseCollection):
                # TODO: support moving collections
                return METHOD_NOT_ALLOWED

            to_item = next(self.Collection.discover(to_path), None)
            if isinstance(to_item, storage.BaseCollection):
                return FORBIDDEN
            to_parent_path = storage.sanitize_path(
                "/%s/" % posixpath.dirname(to_path.strip("/")))
            to_collection = next(
                self.Collection.discover(to_parent_path), None)
            if not to_collection:
                return CONFLICT
            tag = item.collection.get_meta("tag")
            if not tag or tag != to_collection.get_meta("tag"):
                return FORBIDDEN
            if to_item and environ.get("HTTP_OVERWRITE", "F") != "T":
                return PRECONDITION_FAILED
            if (to_item and item.uid != to_item.uid or
                    not to_item and
                    to_collection.path != item.collection.path and
                    to_collection.has_uid(item.uid)):
                return self._webdav_error_response(
                    "C" if tag == "VCALENDAR" else "CR", "no-uid-conflict")
            to_href = posixpath.basename(to_path.strip("/"))
            try:
                self.Collection.move(item, to_collection, to_href)
            except ValueError as e:
                logger.warning(
                    "Bad MOVE request on %r: %s", path, e, exc_info=True)
                return BAD_REQUEST
            return client.NO_CONTENT if to_item else client.CREATED, {}, None

    def do_OPTIONS(self, environ, base_prefix, path, user):
        """Manage OPTIONS request."""
        headers = {
            "Allow": ", ".join(
                name[3:] for name in dir(self) if name.startswith("do_")),
            "DAV": DAV_HEADERS}
        return client.OK, headers, None

    def do_PROPFIND(self, environ, base_prefix, path, user):
        """Manage PROPFIND request."""
        if not self._access(user, path, "r"):
            return NOT_ALLOWED
        try:
            xml_content = self._read_xml_content(environ)
        except RuntimeError as e:
            logger.warning(
                "Bad PROPFIND request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        except socket.timeout as e:
            logger.debug("client timed out", exc_info=True)
            return REQUEST_TIMEOUT
        with self.Collection.acquire_lock("r", user):
            items = self.Collection.discover(
                path, environ.get("HTTP_DEPTH", "0"))
            # take root item for rights checking
            item = next(items, None)
            if not item:
                return NOT_FOUND
            if not self._access(user, path, "r", item):
                return NOT_ALLOWED
            # put item back
            items = itertools.chain([item], items)
            allowed_items = self.collect_allowed_items(items, user)
            headers = {"DAV": DAV_HEADERS,
                       "Content-Type": "text/xml; charset=%s" % self.encoding}
            status, xml_answer = xmlutils.propfind(
                base_prefix, path, xml_content, allowed_items, user)
            if status == client.FORBIDDEN:
                return NOT_ALLOWED
            return status, headers, self._write_xml_content(xml_answer)

    def do_PROPPATCH(self, environ, base_prefix, path, user):
        """Manage PROPPATCH request."""
        if not self._access(user, path, "w"):
            return NOT_ALLOWED
        try:
            xml_content = self._read_xml_content(environ)
        except RuntimeError as e:
            logger.warning(
                "Bad PROPPATCH request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        except socket.timeout as e:
            logger.debug("client timed out", exc_info=True)
            return REQUEST_TIMEOUT
        with self.Collection.acquire_lock("w", user):
            item = next(self.Collection.discover(path), None)
            if not item:
                return NOT_FOUND
            if not self._access(user, path, "w", item):
                return NOT_ALLOWED
            if not isinstance(item, storage.BaseCollection):
                return FORBIDDEN
            headers = {"DAV": DAV_HEADERS,
                       "Content-Type": "text/xml; charset=%s" % self.encoding}
            try:
                xml_answer = xmlutils.proppatch(base_prefix, path, xml_content,
                                                item)
            except ValueError as e:
                logger.warning(
                    "Bad PROPPATCH request on %r: %s", path, e, exc_info=True)
                return BAD_REQUEST
            return (client.MULTI_STATUS, headers,
                    self._write_xml_content(xml_answer))

    def do_PUT(self, environ, base_prefix, path, user):
        """Manage PUT request."""
        if not self._access(user, path, "w"):
            return NOT_ALLOWED
        try:
            content = self._read_content(environ)
        except RuntimeError as e:
            logger.warning("Bad PUT request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        except socket.timeout as e:
            logger.debug("client timed out", exc_info=True)
            return REQUEST_TIMEOUT
        # Prepare before locking
        parent_path = storage.sanitize_path(
            "/%s/" % posixpath.dirname(path.strip("/")))
        permissions = self.Rights.authorized(user, path, "Ww")
        parent_permissions = self.Rights.authorized(user, parent_path, "w")

        def prepare(vobject_items, tag=None, write_whole_collection=None):
            if (write_whole_collection or
                    permissions and not parent_permissions):
                write_whole_collection = True
                content_type = environ.get("CONTENT_TYPE",
                                           "").split(";")[0]
                tags = {value: key
                        for key, value in xmlutils.MIMETYPES.items()}
                tag = storage.predict_tag_of_whole_collection(
                    vobject_items, tags.get(content_type))
                if not tag:
                    raise ValueError("Can't determine collection tag")
                collection_path = storage.sanitize_path(path).strip("/")
            elif (write_whole_collection is not None and
                    not write_whole_collection or
                    not permissions and parent_permissions):
                write_whole_collection = False
                if tag is None:
                    tag = storage.predict_tag_of_parent_collection(
                        vobject_items)
                collection_path = posixpath.dirname(
                    storage.sanitize_path(path).strip("/"))
            props = None
            stored_exc_info = None
            items = []
            try:
                if tag:
                    storage.check_and_sanitize_items(
                        vobject_items, is_collection=write_whole_collection,
                        tag=tag)
                    if write_whole_collection and tag == "VCALENDAR":
                        vobject_components = []
                        vobject_item, = vobject_items
                        for content in ("vevent", "vtodo", "vjournal"):
                            vobject_components.extend(
                                getattr(vobject_item, "%s_list" % content, []))
                        vobject_components_by_uid = itertools.groupby(
                            sorted(vobject_components, key=storage.get_uid),
                            storage.get_uid)
                        for uid, components in vobject_components_by_uid:
                            vobject_collection = vobject.iCalendar()
                            for component in components:
                                vobject_collection.add(component)
                            item = storage.Item(
                                collection_path=collection_path,
                                vobject_item=vobject_collection)
                            item.prepare()
                            items.append(item)
                    elif write_whole_collection and tag == "VADDRESSBOOK":
                        for vobject_item in vobject_items:
                            item = storage.Item(
                                collection_path=collection_path,
                                vobject_item=vobject_item)
                            item.prepare()
                            items.append(item)
                    elif not write_whole_collection:
                        vobject_item, = vobject_items
                        item = storage.Item(collection_path=collection_path,
                                            vobject_item=vobject_item)
                        item.prepare()
                        items.append(item)

                if write_whole_collection:
                    props = {}
                    if tag:
                        props["tag"] = tag
                    if tag == "VCALENDAR" and vobject_items:
                        if hasattr(vobject_items[0], "x_wr_calname"):
                            calname = vobject_items[0].x_wr_calname.value
                            if calname:
                                props["D:displayname"] = calname
                        if hasattr(vobject_items[0], "x_wr_caldesc"):
                            caldesc = vobject_items[0].x_wr_caldesc.value
                            if caldesc:
                                props["C:calendar-description"] = caldesc
                    storage.check_and_sanitize_props(props)
            except Exception:
                stored_exc_info = sys.exc_info()

            # Use generator for items and delete references to free memory
            # early
            def items_generator():
                while items:
                    yield items.pop(0)

            return (items_generator(), tag, write_whole_collection, props,
                    stored_exc_info)

        try:
            vobject_items = tuple(vobject.readComponents(content or ""))
        except Exception as e:
            logger.warning(
                "Bad PUT request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        (prepared_items, prepared_tag, prepared_write_whole_collection,
         prepared_props, prepared_exc_info) = prepare(vobject_items)

        with self.Collection.acquire_lock("w", user):
            item = next(self.Collection.discover(path), None)
            parent_item = next(self.Collection.discover(parent_path), None)
            if not parent_item:
                return CONFLICT

            write_whole_collection = (
                isinstance(item, storage.BaseCollection) or
                not parent_item.get_meta("tag"))

            if write_whole_collection:
                tag = prepared_tag
            else:
                tag = parent_item.get_meta("tag")

            if write_whole_collection:
                if not self.Rights.authorized(user, path, "w" if tag else "W"):
                    return NOT_ALLOWED
            elif not self.Rights.authorized(user, parent_path, "w"):
                return NOT_ALLOWED

            etag = environ.get("HTTP_IF_MATCH", "")
            if not item and etag:
                # Etag asked but no item found: item has been removed
                return PRECONDITION_FAILED
            if item and etag and item.etag != etag:
                # Etag asked but item not matching: item has changed
                return PRECONDITION_FAILED

            match = environ.get("HTTP_IF_NONE_MATCH", "") == "*"
            if item and match:
                # Creation asked but item found: item can't be replaced
                return PRECONDITION_FAILED

            if (tag != prepared_tag or
                    prepared_write_whole_collection != write_whole_collection):
                (prepared_items, prepared_tag, prepared_write_whole_collection,
                 prepared_props, prepared_exc_info) = prepare(
                    vobject_items, tag, write_whole_collection)
            props = prepared_props
            if prepared_exc_info:
                logger.warning(
                    "Bad PUT request on %r: %s", path, prepared_exc_info[1],
                    exc_info=prepared_exc_info)
                return BAD_REQUEST

            if write_whole_collection:
                try:
                    etag = self.Collection.create_collection(
                        path, prepared_items, props).etag
                except ValueError as e:
                    logger.warning(
                        "Bad PUT request on %r: %s", path, e, exc_info=True)
                    return BAD_REQUEST
            else:
                prepared_item, = prepared_items
                if (item and item.uid != prepared_item.uid or
                        not item and parent_item.has_uid(prepared_item.uid)):
                    return self._webdav_error_response(
                        "C" if tag == "VCALENDAR" else "CR",
                        "no-uid-conflict")

                href = posixpath.basename(path.strip("/"))
                try:
                    etag = parent_item.upload(href, prepared_item).etag
                except ValueError as e:
                    logger.warning(
                        "Bad PUT request on %r: %s", path, e, exc_info=True)
                    return BAD_REQUEST

            headers = {"ETag": etag}
            return client.CREATED, headers, None

    def do_REPORT(self, environ, base_prefix, path, user):
        """Manage REPORT request."""
        if not self._access(user, path, "r"):
            return NOT_ALLOWED
        try:
            xml_content = self._read_xml_content(environ)
        except RuntimeError as e:
            logger.warning(
                "Bad REPORT request on %r: %s", path, e, exc_info=True)
            return BAD_REQUEST
        except socket.timeout as e:
            logger.debug("client timed out", exc_info=True)
            return REQUEST_TIMEOUT
        with contextlib.ExitStack() as lock_stack:
            lock_stack.enter_context(self.Collection.acquire_lock("r", user))
            item = next(self.Collection.discover(path), None)
            if not item:
                return NOT_FOUND
            if not self._access(user, path, "r", item):
                return NOT_ALLOWED
            if isinstance(item, storage.BaseCollection):
                collection = item
            else:
                collection = item.collection
            headers = {"Content-Type": "text/xml; charset=%s" % self.encoding}
            try:
                status, xml_answer = xmlutils.report(
                    base_prefix, path, xml_content, collection,
                    lock_stack.close)
            except ValueError as e:
                logger.warning(
                    "Bad REPORT request on %r: %s", path, e, exc_info=True)
                return BAD_REQUEST
            return (status, headers, self._write_xml_content(xml_answer))


_application = None
_application_config_path = None
_application_lock = threading.Lock()


def _init_application(config_path, wsgi_errors):
    global _application, _application_config_path
    with _application_lock:
        if _application is not None:
            return
        log.setup()
        with log.register_stream(wsgi_errors):
            _application_config_path = config_path
            configuration = config.load([config_path] if config_path else [],
                                        ignore_missing_paths=False)
            log.set_level(configuration.get("logging", "level"))
            _application = Application(configuration)


def application(environ, start_response):
    config_path = environ.get("RADICALE_CONFIG",
                              os.environ.get("RADICALE_CONFIG"))
    if _application is None:
        _init_application(config_path, environ["wsgi.errors"])
    if _application_config_path != config_path:
        raise ValueError("RADICALE_CONFIG must not change: %s != %s" %
                         (repr(config_path), repr(_application_config_path)))
    return _application(environ, start_response)
