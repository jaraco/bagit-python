#!/usr/bin/env python

"""
BagIt is a directory, filename convention for bundling an arbitrary set of
files with a manifest, checksums, and additional metadata. More about BagIt
can be found at:

    http://purl.org/net/bagit

bagit.py is a pure python drop in library and command line tool for creating,
and working with BagIt directories:

    import bagit
    bag = bagit.make_bag('example-directory', {'Contact-Name': 'Ed Summers'})
    print bag.entries

Basic usage is to give bag a directory to bag up:

    % bagit.py my_directory

You can bag multiple directories if you wish:

    % bagit.py directory1 directory2

Optionally you can pass metadata intended for the bag-info.txt:

    % bagit.py --source-organization "Library of Congress" directory

For more help see:

    % bagit.py --help
"""

import os
import sys
import hashlib
import logging
import optparse
import multiprocessing
import codecs

from glob import glob
from datetime import date
from itertools import chain

# standard bag-info.txt metadata
_bag_info_headers = [
    'Source-Organization',
    'Organization-Address',
    'Contact-Name',
    'Contact-Phone',
    'Contact-Email',
    'External-Description',
    'External-Identifier',
    'Bag-Size',
    'Bag-Group-Identifier',
    'Bag-Count',
    'Internal-Sender-Identifier',
    'Internal-Sender-Description',
    'BagIt-Profile-Identifier',
    # Bagging Date is autogenerated
    # Payload-Oxum is autogenerated
]

checksum_algos = ['md5', 'sha1']


def make_bag(bag_dir, bag_info=None, processes=1):
    """
    Convert a given directory into a bag. You can pass in arbitrary
    key/value pairs to put into the bag-info.txt metadata file as
    the bag_info dictionary.
    """
    logging.info("creating bag for directory %s" % bag_dir)

    if not os.path.isdir(bag_dir):
        logging.error("no such bag directory %s" % bag_dir)
        raise RuntimeError("no such bag directory %s" % bag_dir)

    old_dir = os.path.abspath(os.path.curdir)
    os.chdir(bag_dir)

    try:
        unbaggable = _can_bag(os.curdir)
        if unbaggable:
            logging.error("no write permissions for the following directories and files: \n%s", unbaggable)
            sys.exit("\nNot all files/folders can be moved.")
        unreadable_dirs, unreadable_files = _can_read(os.curdir)
        if unreadable_dirs or unreadable_files:
            if unreadable_dirs:
                logging.error("The following directories do not have read permissions: \n%s", unreadable_dirs)
            if unreadable_files:
                logging.error("The following files do not have read permissions: \n%s", unreadable_files)
            sys.exit("\nRead permissions are required to calculate file fixities.")
        else:
            logging.info("creating data dir")
            os.mkdir('data')

            for f in os.listdir('.'):
                if f == 'data': continue
                new_f = os.path.join('data', f)
                logging.info("moving %s to %s" % (f, new_f))
                os.rename(f, new_f)

            logging.info("writing manifest-md5.txt")
            Oxum = _make_manifest('manifest-md5.txt', 'data', processes)

            logging.info("writing bagit.txt")
            txt = """BagIt-Version: 0.96\nTag-File-Character-Encoding: UTF-8\n"""
            open("bagit.txt", "wb").write(txt)

            logging.info("writing bag-info.txt")
            bag_info_txt = open("bag-info.txt", "wb")
            if bag_info == None:
                bag_info = {}
            bag_info['Bagging-Date'] = date.strftime(date.today(), "%Y-%m-%d")
            bag_info['Payload-Oxum'] = Oxum
            bag_info['Bag-Software-Agent'] = 'bagit.py <http://github.com/edsu/bagit>'
            headers = bag_info.keys()
            headers.sort()
            for h in headers:
                bag_info_txt.write("%s: %s\n"  % (h, bag_info[h]))
            bag_info_txt.close()

    except Exception, e:
        os.chdir(old_dir)
        logging.error(e)
        raise e

    os.chdir(old_dir)
    return Bag(bag_dir)



class BagError(Exception):
    pass

class BagValidationError(BagError):
    pass

class Bag(object):
    """A representation of a bag."""

    valid_files = ["bagit.txt", "fetch.txt"]
    valid_directories = ['data']

    def __init__(self, path=None):
        super(Bag, self).__init__()
        self.tags = {}
        self.info = {}
        self.entries = {}
        self.algs = []
        self.tag_file_name = None
        self.path = path
        if path:
            # if path ends in a path separator, strip it off
            if path[-1] == os.sep:
                self.path = path[:-1]
            self._open()

    def __str__(self):
        return self.path

    def _open(self):
        # Open the bagit.txt file, and load any tags from it, including
        # the required version and encoding.
        bagit_file_path = os.path.join(self.path, "bagit.txt")

        if not isfile(bagit_file_path):
            raise BagError("No bagit.txt found: %s" % bagit_file_path)

        self.tags = tags = _load_tag_file(bagit_file_path)

        try:
            self.version = tags["BagIt-Version"]
            self.encoding = tags["Tag-File-Character-Encoding"]
        except KeyError, e:
            raise BagError("Missing required tag in bagit.txt: %s" % e)

        if self.version == "0.95":
            self.tag_file_name = "package-info.txt"
        elif self.version == "0.96":
            self.tag_file_name = "bag-info.txt"
        else:
            raise BagError("Unsupported bag version: %s" % self.version)

        if not self.encoding.lower() == "utf-8":
            raise BagValidationError("Unsupported encoding: %s" % self.encoding)

        info_file_path = os.path.join(self.path, self.tag_file_name)
        if os.path.exists(info_file_path):
            self.info = _load_tag_file(info_file_path)

        self._load_manifests()

    def manifest_files(self):
        for filename in ["manifest-%s.txt" % a for a in checksum_algos]:
            f = os.path.join(self.path, filename)
            if isfile(f):
                yield f

    def tagmanifest_files(self):
        for filename in ["tagmanifest-%s.txt" % a for a in checksum_algos]:
            f = os.path.join(self.path, filename)
            if isfile(f):
                yield f

    def compare_manifests_with_fs(self):
        files_on_fs = set(self.payload_files())
        files_in_manifest = set(self.entries.keys())

        return (list(files_in_manifest - files_on_fs),
             list(files_on_fs - files_in_manifest))

    def compare_fetch_with_fs(self):
        """Compares the fetch entries with the files actually
           in the payload, and returns a list of all the files
           that still need to be fetched.
        """

        files_on_fs = set(self.payload_files())
        files_in_fetch = set(self.files_to_be_fetched())

        return list(files_in_fetch - files_on_fs)

    def payload_files(self):
        payload_dir = os.path.join(self.path, "data")

        for dirpath, dirnames, filenames in os.walk(payload_dir):
            for f in filenames:
                # Jump through some hoops here to make the payload files come out
                # looking like data/dir/file, rather than having the entire path.
                rel_path = os.path.join(dirpath, os.path.normpath(f.replace('\\', '/')))
                rel_path = rel_path.replace(self.path + os.path.sep, "", 1)
                yield rel_path

    def fetch_entries(self):
        fetch_file_path = os.path.join(self.path, "fetch.txt")

        if isfile(fetch_file_path):
            fetch_file = open(fetch_file_path, 'rb')

            try:
                for line in fetch_file:
                    parts = line.strip().split(None, 2)
                    yield (parts[0], parts[1], parts[2])
            except Exception, e:
                fetch_file.close()
                raise e

            fetch_file.close()
    def files_to_be_fetched(self):
        for f, size, path in self.fetch_entries():
            yield f 

    def has_oxum(self):
        return self.info.has_key('Payload-Oxum')

    def validate(self, fast=False):
        """Checks the structure and contents are valid. If you supply 
        the parameter fast=True the Payload-Oxum (if present) will 
        be used to check that the payload files are present and 
        accounted for, instead of re-calculating fixities and 
        comparing them against the manifest. By default validate()
        will re-calculate fixities (fast=False).
        """
        self._validate_structure()
        self._validate_bagittxt()
        self._validate_contents(fast=fast)
        return True

    def is_valid(self, fast=False):
        """Returns validation success or failure as boolean.
        Optional fast parameter passed directly to validate().
        """
        try:
            self.validate(fast=fast)
        except BagError, e:
            return False
        return True

    def _load_manifests(self):
        for manifest_file in self.manifest_files():
            alg = os.path.basename(manifest_file).replace("manifest-", "").replace(".txt", "")
            self.algs.append(alg)

            manifest_file = open(manifest_file, 'rb')

            try:
                for line in manifest_file:
                    line = line.strip()

                    # Ignore blank lines and comments.
                    if line == "" or line.startswith("#"): continue

                    entry = line.split(None, 1)

                    # Format is FILENAME *CHECKSUM
                    if len(entry) != 2:
                        logging.error("%s: Invalid %s manifest entry: %s", self, alg, line)
                        continue

                    entry_hash = entry[0]
                    entry_path = os.path.normpath(entry[1].lstrip("*"))

                    if self.entries.has_key(entry_path):
                        if self.entries[entry_path].has_key(alg):
                            logging.warning("%s: Duplicate %s manifest entry: %s", self, alg, entry_path)

                        self.entries[entry_path][alg] = entry_hash
                    else:
                        self.entries[entry_path] = {}
                        self.entries[entry_path][alg] = entry_hash
            finally:
                manifest_file.close()

    def _validate_structure(self):
        """Checks the structure of the bag, determining if it conforms to the
           BagIt spec. Returns true on success, otherwise it will raise
           a BagValidationError exception.
        """
        self._validate_structure_payload_directory()
        self._validate_structure_tag_files()

    def _validate_structure_payload_directory(self):
        data_dir_path = os.path.join(self.path, "data")

        if not isdir(data_dir_path):
            raise BagValidationError("Missing data directory")

    def _validate_structure_tag_files(self):
        # Note: we deviate somewhat from v0.96 of the spec in that it allows
        # other files and directories to be present in the base directory
        # see 
        if len(list(self.manifest_files())) == 0:
            raise BagValidationError("Missing manifest file")
        if "bagit.txt" not in os.listdir(self.path):
            raise BagValidationError("Missing bagit.txt")

    def _validate_contents(self, fast=False):
        if fast and not self.has_oxum():
            raise BagValidationError("cannot validate Bag with fast=True if Bag lacks a Payload-Oxum")
        self._validate_oxum()    # Fast
        if not fast:
            self._validate_entries() # *SLOW*

    def _validate_oxum(self):
        oxum = self.info.get('Payload-Oxum')
        if oxum == None: return

        byte_count, file_count = oxum.split('.', 1)

        if not byte_count.isdigit() or not file_count.isdigit():
            raise BagError("Invalid oxum: %s" % oxum)

        byte_count = long(byte_count)
        file_count = long(file_count)
        total_bytes = 0
        total_files = 0

        for payload_file in self.payload_files():
            payload_file = os.path.join(self.path, payload_file)
            total_bytes += os.stat(payload_file).st_size
            total_files += 1

        if file_count != total_files or byte_count != total_bytes:
            raise BagValidationError("Oxum error.  Found %s files and %s bytes on disk; expected %s files and %s bytes." % (total_files, total_bytes, file_count, byte_count))

    def _validate_entries(self):
        """
        Verify that the actual file contents match the recorded hashes stored in the manifest files
        """
        errors = list()

        # First we'll make sure there's no mismatch between the filesystem
        # and the list of files in the manifest(s)
        only_in_manifests, only_on_fs = self.compare_manifests_with_fs()
        for path in only_in_manifests:
            logging.warning("%s: exists in manifest but not in filesystem", path)
            errors.append(path)
        for path in only_on_fs:
            logging.warning("%s: exists in filesystem but not in manifests", path)
            errors.append(path)

        # To avoid the overhead of reading the file more than once or loading
        # potentially massive files into memory we'll create a dictionary of
        # hash objects so we can open a file, read a block and pass it to
        # multiple hash objects

        hashers = {}
        for alg in self.algs:
            try:
                hashers[alg] = hashlib.new(alg)
            except KeyError:
                logging.warning("Unable to validate file contents using unknown %s hash algorithm", alg)

        if not hashers:
            raise RuntimeError("%s: Unable to validate bag contents: none of the hash algorithms in %s are supported!" % (self, self.algs))

        for rel_path, hashes in self.entries.items():
            full_path = os.path.join(self.path, rel_path)

            # Create a clone of the default empty hash objects:
            f_hashers = dict(
                (alg, hashlib.new(alg)) for alg, h in hashers.items() if alg in hashes
            )

            try:
                f_hashes = self._calculate_file_hashes(full_path, f_hashers)
            except BagValidationError, e:
                raise e
            # Any unhandled exceptions are probably fatal
            except:
                logging.exception("unable to calculate file hashes for %s: %s", self, full_path)
                raise

            for alg, computed_hash in f_hashes.items():
                stored_hash = hashes[alg]
                if stored_hash != computed_hash:
                    logging.warning("%s: stored hash %s doesn't match calculated hash %s", full_path, stored_hash, computed_hash)
                    errors.append("%s (%s)" % (full_path, alg))

        if errors:
            raise BagValidationError("%s: %d files failed checksum validation: %s" % (self, len(errors), errors))

    def _validate_bagittxt(self):
        """
        Verify that bagit.txt conforms to specification
        """
        bagit_file_path = os.path.join(self.path, "bagit.txt")
        bagit_file = open(bagit_file_path, 'rb')
        try:
            first_line = bagit_file.readline()
            if first_line.startswith(codecs.BOM_UTF8):
                raise BagValidationError("bagit.txt must not contain a byte-order mark")
        finally:
            bagit_file.close()


    def _calculate_file_hashes(self, full_path, f_hashers):
        """
        Returns a dictionary of (algorithm, hexdigest) values for the provided
        filename
        """
        if not os.path.exists(full_path):
            raise BagValidationError("%s does not exist" % full_path)

        f = open(full_path, 'rb')

        f_size = os.stat(full_path).st_size

        while True:
            block = f.read(1048576)
            if not block:
                break
            [ i.update(block) for i in f_hashers.values() ]
        f.close()

        return dict(
            (alg, h.hexdigest()) for alg, h in f_hashers.items()
        )

def _load_tag_file(tag_file_name):
    tag_file = open(tag_file_name, 'rb')

    try:
        return dict(_parse_tags(tag_file))
    finally:
        tag_file.close()

def _parse_tags(file):
    """Parses a tag file, according to RFC 2822.  This
       includes line folding, permitting extra-long
       field values.

       See http://www.faqs.org/rfcs/rfc2822.html for
       more information.
    """

    tag_name = None
    tag_value = None

    # Line folding is handled by yielding values
    # only after we encounter the start of a new
    # tag, or if we pass the EOF.
    for num, line in enumerate(file):
        # If byte-order mark ignore it for now.
        if 0 == num:
            if line.startswith(codecs.BOM_UTF8):
                line = line.lstrip(codecs.BOM_UTF8)

        # Skip over any empty or blank lines.
        if len(line) == 0 or line.isspace():
            continue

        if line[0].isspace(): # folded line
            tag_value += line.strip()
        else:
            # Starting a new tag; yield the last one.
            if tag_name:
                yield (tag_name, tag_value)

            parts = line.strip().split(':', 1)
            tag_name = parts[0].strip()
            tag_value = parts[1].strip()

    # Passed the EOF.  All done after this.
    if tag_name:
        yield (tag_name, tag_value)


def _make_manifest(manifest_file, data_dir, processes):
    logging.info('writing manifest with %s processes' % processes)

    # avoid using multiprocessing unless it is required since
    # multiprocessing doesn't work in some environments (mod_wsgi, etc)

    if processes > 1:
        pool = multiprocessing.Pool(processes=processes)
        checksums = pool.map(_manifest_line, _walk(data_dir))
        pool.close()
        pool.join()
    else:
        checksums = map(_manifest_line, _walk(data_dir))

    manifest = open(manifest_file, 'wb')
    num_files = 0
    total_bytes = 0

    for digest, filename, bytes in checksums:
        num_files += 1
        total_bytes += bytes
        manifest.write("%s  %s\n" % (digest, filename))
    manifest.close()
    return "%s.%s" % (total_bytes, num_files)

def _walk(data_dir):
    for dirpath, dirnames, filenames in os.walk(data_dir):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            # BagIt spec requires manifest to always use '/' as path separator
            if os.path.sep != '/':
                parts = path.split(os.path.sep)
                path = '/'.join(parts)
            yield path

def _can_bag(test_dir):
    """returns (unwriteable files/folders)
    """
    unwriteable = []
    for inode in os.listdir(test_dir):
        if not os.access(os.path.join(test_dir, inode), os.W_OK):
            unwriteable.append(os.path.join(os.path.abspath(test_dir), inode))
    return tuple(unwriteable)

def _can_read(test_dir):
    """
    returns ((unreadable_dirs), (unreadable_files))
    """
    unreadable_dirs = []
    unreadable_files = []
    for dirpath, dirnames, filenames in os.walk(test_dir):
        for dn in dirnames:
            if not os.access(os.path.join(dirpath, dn), os.R_OK):
                unreadable_dirs.append(os.path.join(dirpath, dn))
        for fn in filenames:
            if not os.access(os.path.join(dirpath, fn), os.R_OK):
                unreadable_files.append(os.path.join(dirpath, fn))
    return (tuple(unreadable_dirs), tuple(unreadable_files))

def _manifest_line(filename):
    fh = open(filename, 'rb')
    m = hashlib.md5()
    total_bytes = 0
    while True:
        bytes = fh.read(16384)
        total_bytes += len(bytes)
        if not bytes: break
        m.update(bytes)
    fh.close()
    return (m.hexdigest(), filename, total_bytes)


# following code is used for command line program

class BagOptionParser(optparse.OptionParser):
    def __init__(self, *args, **opts):
        self.bag_info = {}
        optparse.OptionParser.__init__(self, *args, **opts)

def _bag_info_store(option, opt, value, parser):
    opt = opt.lstrip('--')
    opt_caps = '-'.join([o.capitalize() for o in opt.split('-')])
    parser.bag_info[opt_caps] = value

def _make_opt_parser():
    parser = BagOptionParser(usage='usage: %prog [options] dir1 dir2 ...')
    parser.add_option('--processes', action='store', type="int",
                      dest='processes', default=1,
                      help='parallelize checksums generation')
    parser.add_option('--log', action='store', dest='log')
    parser.add_option('--quiet', action='store_true', dest='quiet')
    parser.add_option('--validate', action='store_true', dest='validate')
    parser.add_option('--fast', action='store_true', dest='fast')

    for header in _bag_info_headers:
        parser.add_option('--%s' % header.lower(), type="string",
                          action='callback', callback=_bag_info_store)
    return parser

def _configure_logging(opts):
    log_format="%(asctime)s - %(levelname)s - %(message)s"
    if opts.quiet:
        level = logging.ERROR
    else:
        level = logging.INFO
    if opts.log:
        logging.basicConfig(filename=opts.log, level=level, format=log_format)
    else:
        logging.basicConfig(level=level, format=log_format)

def isfile(path):
    return os.path.isfile(path)

def isdir(path):
    if path.startswith('http://'):
        return open(path).getcode() == 200
    return os.path.isdir(path)

if __name__ == '__main__':
    opt_parser = _make_opt_parser()
    opts, args = opt_parser.parse_args()
    _configure_logging(opts)
    log = logging.getLogger()

    rc = 0
    for bag_dir in args:

        # validate the bag
        if opts.validate:
            try:
                bag = Bag(bag_dir)
                # validate throws a BagError or BagValidationError
                valid = bag.validate(fast=opts.fast)
                if opts.fast:
                    log.info("%s valid according to Payload-Oxum" % bag_dir)
                else:
                    log.info("%s is valid" % bag_dir)
            except BagError, e:
                log.info("%s is invalid: %s" % (bag_dir, e))
                rc = 1

        # make the bag
        else:
            make_bag(bag_dir, bag_info=opt_parser.bag_info, 
                     processes=opts.processes)

        sys.exit(rc)
