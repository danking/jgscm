import base64
import os
import uuid

from gcloud.exceptions import NotFound, Forbidden, BadRequest
from gcloud.storage import Client as GSClient, Blob
import nbformat
from notebook.services.contents.checkpoints import Checkpoints, \
    GenericCheckpointsMixin
from notebook.services.contents.manager import ContentsManager
from tornado import web
from tornado.escape import url_unescape
from traitlets import Any, Bool, Int, Unicode


class GoogleStorageCheckpoints(GenericCheckpointsMixin, Checkpoints):
    checkpoint_dir = Unicode(
        ".ipynb_checkpoints",
        config=True,
        help="""The directory name in which to keep file checkpoints

            This is a path relative to the file"s own directory.

            By default, it is .ipynb_checkpoints
            """,
    )
    checkpoint_bucket = Unicode(
        "", config=True, help="The bucket name where to keep file checkpoints."
                              " If empty, the current bucket is used."
    )
    
    def create_file_checkpoint(self, content, format, path):
        """Create a checkpoint of the current state of a file

        Returns a checkpoint model for the new checkpoint.
        """
        checkpoint_id = str(uuid.uuid4())
        cp = self._get_checkpoint_path(checkpoint_id, path)
        self.log.debug("creating checkpoint %s for %s as %s",
                       checkpoint_id, path, cp)
        blob = self.parent._save_file(cp, content, format)
        return {
            "id": checkpoint_id,
            "last_modified": blob.updated,
        }

    def create_notebook_checkpoint(self, nb, path):
        """Create a checkpoint of the current state of a file

        Returns a checkpoint model for the new checkpoint.
        """
        checkpoint_id = str(uuid.uuid4())
        cp = self._get_checkpoint_path(checkpoint_id, path)
        self.log.debug("creating checkpoint %s for %s as %s",
                       checkpoint_id, path, cp)
        blob = self.parent._save_notebook(cp, nb)
        return {
            "id": checkpoint_id,
            "last_modified": blob.updated,
        }

    def get_file_checkpoint(self, checkpoint_id, path):
        """Get the content of a checkpoint for a non-notebook file.

         Returns a dict of the form:
         {
             "type": "file",
             "content": <str>,
             "format": {"text","base64"},
         }
        """
        self.log.info("restoring %s from checkpoint %s", path, checkpoint_id)
        cp = self._get_checkpoint_path(checkpoint_id, path)
        exists, blob = self.parent._fetch(cp)
        if not exists:
            raise web.HTTPError(404, u"No such checkpoint: %s for %s" % (
                checkpoint_id, path))
        content, fmt = self.parent._read_file(blob, None)
        return {
            "type": "file",
            "content": content,
            "format": fmt
        }

    def get_notebook_checkpoint(self, checkpoint_id, path):
        """Get the content of a checkpoint for a notebook.

        Returns a dict of the form:
        {
            "type": "notebook",
            "content": <output of nbformat.read>,
        }
        """
        self.log.info("restoring %s from checkpoint %s", path, checkpoint_id)
        cp = self._get_checkpoint_path(checkpoint_id, path)
        exists, blob = self.parent._fetch(cp)
        if not exists:
            raise web.HTTPError(404, u"No such checkpoint: %s for %s" % (
                checkpoint_id, path))
        nb = self.parent._read_notebook(blob)
        return {
            "type": "notebook",
            "content": nb
        }

    def rename_checkpoint(self, checkpoint_id, old_path, new_path):
        """Rename a single checkpoint from old_path to new_path."""
        old_cp = self._get_checkpoint_path(checkpoint_id, old_path)
        new_cp = self._get_checkpoint_path(checkpoint_id, new_path)
        self.parent.rename_file(old_cp, new_cp)

    def delete_checkpoint(self, checkpoint_id, path):
        """delete a checkpoint for a file"""
        cp = self._get_checkpoint_path(checkpoint_id, path)
        self.parent.delete_file(cp)

    def list_checkpoints(self, path):
        """Return a list of checkpoints for a given file"""
        cp = self._get_checkpoint_path(None, path)
        bucket_name, bucket_path = self.parent._parse_path(cp)
        try:
            bucket = self.parent._get_bucket(bucket_name)
            it = bucket.list_blobs(prefix=bucket_path, delimiter="/",
                                   max_results=self.parent.max_list_size)
            checkpoints = [{
                "id": os.path.splitext(file.path)[0][-36:],
                "last_modified": file.updated,
            } for file in it]
        except NotFound:
            return []
        checkpoints.sort(key=lambda c: c["last_modified"], reverse=True)
        self.log.debug("list_checkpoints: %s: %s", path, checkpoints)
        return checkpoints

    def _get_checkpoint_path(self, checkpoint_id, path):
        if path.startswith("/"):
            path = path[1:]
        bucket_name, bucket_path = self.parent._parse_path(path)
        if self.checkpoint_bucket:
            bucket_name = self.checkpoint_bucket
        slash = bucket_path.rfind("/") + 1
        name, ext = os.path.splitext(bucket_path[slash:])
        if checkpoint_id is not None:
            return "%s/%s%s/%s-%s%s" % (
                bucket_name, bucket_path[:slash], self.checkpoint_dir, name,
                checkpoint_id, ext)
        return "%s/%s%s/%s" % (bucket_name, bucket_path[:slash],
                               self.checkpoint_dir, name)


class JupyterGoogleStorageContentManager(ContentsManager):
    project = Unicode(
        "", config=True,
        help="The name of the project in Google Cloud to use. If you do not "
             "set this parameter, gcloud will pick the default project "
             "from the execution context if it exists."
    )
    keyfile = Unicode(
        "", config=True,
        help="The path to the Google Cloud API JSON keyfile which is needed "
             "for authorization. If you do not set this parameter, "
             "gcloud will be OK if the default project exists."
    )
    max_list_size = Int(1024, config=True, help="list_blobs() limit")
    cache_buckets = Bool(True, config=True,
                         help="Value indicating whether to cache the bucket "
                              "objects for faster operations.")
    # redefine untitled_directory to change the default value
    untitled_directory = Unicode(
        "untitled-folder", config=True,
        help="The base name used when creating untitled directories.")
    post_save_hook = Any(None, config=True,
                         help="""Python callable or importstring thereof

            to be called on the path of a file just saved.

            This can be used to process the file on disk,
            such as converting the notebook to a script or HTML via nbconvert.

            It will be called as (all arguments passed by keyword)::

                hook(os_path=path, model=model, contents_manager=instance)

            - path: the GCS path to the file just written
            - model: the model representing the file
            - contents_manager: this ContentsManager instance
            """
                         )

    def debug_args(fn):
        def wrapped_fn(self, *args, **kwargs):
            self.log.debug("call %s(%s%s%s)", fn.__name__,
                           ", ".join(repr(a) for a in args),
                           ", " if args and kwargs else "",
                           ", ".join("%s=%r" % p for p in kwargs.items()))
            result = fn(self, *args, **kwargs)
            self.log.debug("result %s %s", fn.__name__, result)
            return result

        return wrapped_fn

    @debug_args
    def is_hidden(self, path):
        if path == "":
            return False
        try:
            bucket_name, bucket_path = self._parse_path(path)
        except ValueError:
            return True
        try:
            bucket = self._get_bucket(bucket_name)
        except Forbidden:
            return True
        return bucket is None

    @debug_args
    def file_exists(self, path=""):
        if path == "":
            return False
        if path.startswith("/"):
            path = path[1:]
        try:
            bucket_name, bucket_path = self._parse_path(path)
        except ValueError:
            return False
        bucket = self._get_bucket(bucket_name)
        if bucket is None or bucket_path == "":
            return False
        if bucket_path.endswith("/"):
            # blob may exist but we treat such as directories
            return False
        return bucket.blob(bucket_path).exists()

    @debug_args
    def dir_exists(self, path):
        if path.startswith("/"):
            path = path[1:]
        if path == "":
            return True
        if not path.endswith("/"):
            path += "/"
        return self._fetch(path, content=False)[0]

    @debug_args
    def get(self, path, content=True, type=None, format=None):
        if isinstance(path, Blob):
            obj = path
            path = self._get_blob_path(obj)
        elif path.startswith("/"):
            path = path[1:]
        if "/" not in path or path.endswith("/") or type == "directory":
            if type not in (None, "directory"):
                raise web.HTTPError(
                    400, u"%s is not a directory" % path, reason="bad type")
            if "/" in path and not path.endswith("/"):
                path += "/"
            exists, members = self._fetch(path, content=content)
            if not exists:
                raise web.HTTPError(404, u"No such directory: %s" % path)
            model = self._dir_model(path, members, content=content)
        else:
            exists, blob = self._fetch(path)
            if not exists:
                raise web.HTTPError(404, u"No such file: %s" % path)
            if type == "notebook" or (type is None and path.endswith(".ipynb")):
                model = self._notebook_model(blob, content=content)
            else:
                model = self._file_model(blob, content=content, format=format)
        return model

    @debug_args
    def save(self, model, path):
        if path.startswith("/"):
            path = path[1:]
        if "type" not in model:
            raise web.HTTPError(400, u"No file type provided")
        if "content" not in model and model["type"] != "directory":
            raise web.HTTPError(400, u"No file content provided")
        bucket_name, bucket_path = self._parse_path(path)
        if bucket_path == "" and model["type"] != "directory":
            raise web.HTTPError(403, u"You may only create directories "
                                     u"(buckets) at the root level.")
        if bucket_path != "" and model["type"] == "directory":
            path += "/"
        self.log.debug("Saving %s", path)

        self.run_pre_save_hook(model=model, path=path)
        
        try:
            if model["type"] == "notebook":
                nb = nbformat.from_dict(model["content"])
                self.check_and_sign(nb, path)
                self._save_notebook(path, nb)
                # One checkpoint should always exist for notebooks.
                if not self.checkpoints.list_checkpoints(path):
                    self.create_checkpoint(path)
            elif model["type"] == "file":
                # Missing format will be handled internally by _save_file.
                self._save_file(path, model["content"], model.get("format"))
            elif model["type"] == "directory":
                self._save_directory(path, model)
            else:
                raise web.HTTPError(
                    00, u"Unhandled contents type: %s" % model["type"])
        except web.HTTPError:
            raise
        except Exception as e:
            self.log.error(u"Error while saving file: %s %s", path, e,
                           exc_info=True)
            raise web.HTTPError(
                500, u"Unexpected error while saving file: %s %s" % (path, e))

        validation_message = None
        if model["type"] == "notebook":
            self.validate_notebook_model(model)
            validation_message = model.get("message", None)

        model = self.get(path, content=False)
        if validation_message:
            model["message"] = validation_message

        self.run_post_save_hook(model=model, os_path=path)

        return model

    @debug_args
    def delete_file(self, path):
        if path.startswith("/"):
            path = path[1:]
        bucket_name, bucket_path = self._parse_path(path)
        bucket = self._get_bucket(bucket_name, throw=True)
        if bucket_path == "":
            bucket.delete()
            del self._bucket_cache[bucket_name]
            return
        it = bucket.list_blobs(prefix=bucket_path, delimiter="/",
                               max_results=self.max_list_size)
        files = list(it)
        folders = [f[:-1] for f in it.prefixes]
        bucket.delete_blobs(files)
        if not path.endswith("/"):
            path += "/"
        for folder in folders:
            self.delete_file(path + folder)

    @debug_args
    def rename_file(self, old_path, new_path):
        if old_path.startswith("/"):
            old_path = old_path[1:]
        if new_path.startswith("/"):
            new_path = new_path[1:]
        old_bucket_name, old_bucket_path = self._parse_path(old_path)
        old_bucket = self._get_bucket(old_bucket_name, throw=True)
        new_bucket_name, new_bucket_path = self._parse_path(new_path)
        new_bucket = self._get_bucket(new_bucket_name, throw=True)
        old_blob = old_bucket.get_blob()
        if old_bucket_name == new_bucket_name:
            if old_blob is not None:
                old_bucket.rename_blob(old_blob, new_bucket_path)
                return
            it = old_bucket.list_blobs(prefix=old_bucket_path, delimiter="/",
                                       max_results=self.max_list_size)
            old_blobs = list(it)
            folders = [f[:-1] for f in it.prefixes]
            for ob in old_blobs:
                old_bucket.rename_blob(ob, new_bucket_path + "/" +
                                       self._get_blob_name(ob))
            for f in folders:
                self.rename_file(old_bucket_path + "/" + f,
                                 new_bucket_path + "/" + f)
            return
        if old_blob is not None:
            old_bucket.copy_blob(old_blob, new_bucket, new_bucket_path)
            old_bucket.delete_blob(old_blob)
            return
        it = old_bucket.list_blobs(prefix=old_bucket_path, delimiter="/",
                                   max_results=self.max_list_size)
        old_blobs = list(it)
        folders = [f[:-1] for f in it.prefixes]
        for ob in old_blobs:
            old_bucket.copy_blob(ob, new_bucket, new_bucket_path + "/" +
                                 self._get_blob_name(ob))
            old_bucket.delete_blob(old_blob)
        for f in folders:
            self.rename_file(old_bucket_path + "/" + f,
                             new_bucket_path + "/" + f)

    @property
    def client(self):
        try:
            return self._client
        except AttributeError:
            if not self.project:
                self._client = GSClient()
            else:
                self._client = GSClient.from_service_account_json(
                    self.keyfile, project=self.project)
            return self._client

    def run_post_save_hook(self, model, os_path):
        """Run the post-save hook if defined, and log errors"""
        if self.post_save_hook:
            try:
                self.log.debug("Running post-save hook on %s", os_path)
                self.post_save_hook(os_path=path, model=model, contents_manager=self)
            except Exception:
                self.log.error("Post-save hook failed on %s", os_path, exc_info=True)

    def _checkpoints_class_default(self):
        return GoogleStorageCheckpoints

    def _get_bucket(self, name, throw=False):
        if not self.cache_buckets:
            try:
                return self.client.get_bucket(name)
            except NotFound:
                if throw:
                    raise
                return None
        try:
            cache = self._bucket_cache
        except AttributeError:
            self._bucket_cache = cache = {}
        try:
            return cache[name]
        except KeyError:
            try:
                bucket = self.client.get_bucket(name)
            except (BadRequest, NotFound):
                if throw:
                    raise
                return None
            cache[name] = bucket
            return bucket

    @staticmethod
    def _parse_path(path):
        try:
            bucket, name = path.split("/", 1)
        except ValueError:
            bucket = path
            name = ""
        return bucket, name

    @staticmethod
    def _get_blob_path(blob):
        path = url_unescape(blob.path)
        path = path[3:]  # /b/
        path = path.replace("/o/", "/", 1)
        return path
    
    @staticmethod
    def _get_blob_name(blob):
        return url_unescape(blob.path).rsplit("/", 1)[-1]

    @staticmethod
    def _get_dir_name(path):
        if path.endswith("/"):
            path = path[:-1]
        return path.rsplit("/", 1)[-1]

    @debug_args
    def _fetch(self, path, content=True):
        if path == "":
            buckets = self.client.list_buckets()
            return True, ([], [b.name for b in buckets])
        try:
            bucket_name, bucket_path = self._parse_path(path)
        except ValueError:
            return False, None
        try:
            bucket = self._get_bucket(bucket_name)
        except Forbidden:
            return True, None
        if bucket is None:
            return False, None
        if bucket_path == "" or bucket_path.endswith("/"):
            if bucket_path != "":
                exists = bucket.blob(bucket_path).exists()
                if exists and not content:
                    return True, None
            # blob may not exist but at the same time be a part of a path
            max_list_size = self.max_list_size if content else 1
            try:
                it = bucket.list_blobs(prefix=bucket_path, delimiter="/",
                                       max_results=max_list_size)
                files = list(it)
            except NotFound:
                del self._bucket_cache[bucket_name]
                return False, None
            folders = [f[:-1] for f in it.prefixes]
            return (bool(files or folders or bucket_path == ""),
                    (files, folders) if content else None)
        if not content:
            return bucket.blob(bucket_path).exists, None
        blob = bucket.get_blob(bucket_path)
        return blob is not None, blob

    def _base_model(self, blob):
        """Build the common base of a contents model"""
        last_modified = blob.updated
        created = last_modified
        model = {
            "name": self._get_blob_name(blob),
            "path": self._get_blob_path(blob),
            "last_modified": last_modified,
            "created": created,
            "content": None,
            "format": None,
            "mimetype": blob.content_type,
            "writable": True
        }
        return model
    
    def _read_file(self, blob, format):
        """Read a non-notebook file.

        blob: instance of :class:`gcloud.storage.Blob`.
        format:
          If "text", the contents will be decoded as UTF-8.
          If "base64", the raw bytes contents will be encoded as base64.
          If not specified, try to decode as UTF-8, and fall back to base64
        """
        bcontent = blob.download_as_string()

        if format is None or format == "text":
            # Try to interpret as unicode if format is unknown or if unicode
            # was explicitly requested.
            try:
                return bcontent.decode("utf8"), "text"
            except UnicodeError:
                if format == "text":
                    raise web.HTTPError(
                        400, "%s is not UTF-8 encoded" %
                             self._get_blob_path(blob),
                        reason="bad format",
                    )
        return base64.encodebytes(bcontent).decode("ascii"), "base64"
    
    def _file_model(self, blob, content=True, format=None):
        """Build a model for a file

        if content is requested, include the file contents.

        format:
          If "text", the contents will be decoded as UTF-8.
          If "base64", the raw bytes contents will be encoded as base64.
          If not specified, try to decode as UTF-8, and fall back to base64
        """
        model = self._base_model(blob)
        model["type"] = "file"

        if content:
            content, format = self._read_file(blob, format)
            if model["mimetype"] is None:
                default_mime = {
                    "text": "text/plain",
                    "base64": "application/octet-stream"
                }[format]
                model["mimetype"] = default_mime

            model.update(
                content=content,
                format=format,
            )

        return model

    def _read_notebook(self, blob):
        data = blob.download_as_string().decode("utf-8")
        nb = nbformat.reads(data, as_version=4)
        self.mark_trusted_cells(nb, self._get_blob_path(blob))
        return nb

    def _notebook_model(self, blob, content=True):
        """Build a notebook model

        if content is requested, the notebook content will be populated
        as a JSON structure (not double-serialized)
        """
        model = self._base_model(blob)
        model["type"] = "notebook"
        if content:
            nb = self._read_notebook(blob)
            model["content"] = nb
            model["format"] = "json"
            self.validate_notebook_model(model)
        return model
    
    def _dir_model(self, path, members, content=True):
        """Build a model for a directory

        if content is requested, will include a listing of the directory
        """
        model = {
            "type": "directory",
            "name": self._get_dir_name(path),
            "path": path,
            "last_modified": "",
            "created": "",
            "content": None,
            "format": None,
            "mimetype": "application/x-directory",
            "writable": path and (
                members is not None or not self.is_hidden(path))
        }
        if content:
            blobs, folders = members
            model["content"] = contents = []
            for blob in blobs:
                if self._get_blob_path(blob) != path and \
                        self.should_list(self._get_blob_name(blob)):
                    contents.append(self.get(
                        path=blob,
                        content=False)
                    )
            if path != "":
                tmpl = "%s/%%s/" % self._parse_path(path)[0]
            else:
                tmpl = "%s"
            _, this = self._parse_path(path)
            for folder in folders:
                if self.should_list(folder) and folder != this:
                    contents.append(self.get(
                        path=tmpl % folder,
                        content=False)
                    )
            model["format"] = "json"

        return model

    def _save_notebook(self, path, nb):
        bucket_name, bucket_path = self._parse_path(path)
        bucket = self._get_bucket(bucket_name, throw=True)
        data = nbformat.writes(nb, version=nbformat.NO_CONVERT)
        blob = bucket.blob(bucket_path)
        blob.upload_from_string(data, "application/x-ipynb+json")
        return blob

    def _save_file(self, path, content, format):
        """Save content of a generic file."""
        bucket_name, bucket_path = self._parse_path(path)
        bucket = self._get_bucket(bucket_name, throw=True)

        if format not in {"text", "base64"}:
            raise web.HTTPError(
                400,
                u"Must specify format of file contents as \"text\" or "
                u"\"base64\"",
            )
        try:
            if format == "text":
                bcontent = content.encode("utf8")
            else:
                b64_bytes = content.encode("ascii")
                bcontent = base64.decodebytes(b64_bytes)
        except Exception as e:
            raise web.HTTPError(
                400, u"Encoding error saving %s: %s" % (path, e)
            )
        blob = bucket.blob(bucket_path)
        blob.upload_from_string(bcontent)
        return blob

    def _save_directory(self, path, model):
        """create a directory"""
        exists, obj = self._fetch(path)
        if exists:
            if isinstance(obj, Blob):
                raise web.HTTPError(400, u"Not a directory: %s" % path)
            else:
                self.log.debug("Directory %r already exists", path)
                return
        bucket_name, bucket_path = self._parse_path(path)
        if bucket_path == "":
            self.client.create_bucket(bucket_name)
        else:
            bucket = self._get_bucket(bucket_name, throw=True)
            bucket.blob(bucket_path + "/").upload_from_string(
                b"", content_type="application/x-directory")

    debug_args = staticmethod(debug_args)