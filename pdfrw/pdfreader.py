# A part of pdfrw (pdfrw.googlecode.com)
# Copyright (C) 2006-2009 Patrick Maupin, Austin, Texas
# MIT license -- See LICENSE.txt for details

'''
The PdfReader class reads an entire PDF file into memory and
parses the top-level container objects.  (It does not parse
into streams.)  The object subclasses PdfDict, and the
document pages are stored in a list in the pages attribute
of the object.
'''
import gc
import struct

from pdfrw.errors import PdfParseError, log
from pdfrw.tokens import PdfTokens
from pdfrw.objects import PdfDict, PdfArray, PdfName, PdfObject, PdfIndirect
from pdfrw.uncompress import uncompress


class PdfReader(PdfDict):

    warned_bad_stream_start = False  # Use to keep from spewing warnings
    warned_bad_stream_end = False  # Use to keep from spewing warnings

    def findindirect(self, objnum, gennum, PdfIndirect=PdfIndirect, int=int):
        ''' Return a previously loaded indirect object, or create
            a placeholder for it.
        '''
        key = int(objnum), int(gennum)
        result = self.indirect_objects.get(key)
        if result is None:
            self.indirect_objects[key] = result = PdfIndirect(key)
            self.deferred_objects.add(key)
            result._loader = self.loadindirect
        return result

    def readarray(self, source, PdfArray=PdfArray):
        ''' Found a [ token.  Parse the tokens after that.
        '''
        specialget = self.special.get
        result = []
        pop = result.pop
        append = result.append

        for value in source:
            if value in ']R':
                if value == ']':
                    break
                generation = pop()
                value = self.findindirect(pop(), generation)
            else:
                func = specialget(value)
                if func is not None:
                    value = func(source)
            append(value)
        return PdfArray(result)

    def readdict(self, source, PdfDict=PdfDict):
        ''' Found a << token.  Parse the tokens after that.
        '''
        specialget = self.special.get
        result = PdfDict()
        next = source.next

        tok = next()
        while tok != '>>':
            if not tok.startswith('/'):
                source.exception('Expected PDF /name object')
            key = tok
            value = next()
            func = specialget(value)
            if func is not None:
                value = func(source)
                tok = next()
            else:
                tok = next()
                if value.isdigit() and tok.isdigit():
                    if next() != 'R':
                        source.exception('Expected "R" following two integers')
                    value = self.findindirect(value, tok)
                    tok = next()
            result[key] = value
        return result

    def empty_obj(self, source, PdfObject=PdfObject):
        ''' Some silly git put an empty object in the
            file.  Back up so the caller sees the endobj.
        '''
        source.floc = source.tokstart

    def badtoken(self, source):
        ''' Didn't see that coming.
        '''
        source.exception('Unexpected delimiter')

    def findstream(self, obj, tok, source, PdfDict=PdfDict,
                   isinstance=isinstance, len=len):
        ''' Figure out if there is a content stream
            following an object, and return the start
            pointer to the content stream if so.

            (We can't read it yet, because we might not
            know how long it is, because Length might
            be an indirect object.)
        '''

        isdict = isinstance(obj, PdfDict)
        if not isdict or tok != 'stream':
            source.exception("Expected 'endobj'%s token",
                             isdict and " or 'stream'" or '')
        fdata = source.fdata
        startstream = source.tokstart + len(tok)
        gotcr = fdata[startstream] == '\r'
        startstream += gotcr
        gotlf = fdata[startstream] == '\n'
        startstream += gotlf
        if not gotlf:
            if not gotcr:
                source.exception(r'stream keyword not followed by \n')
            if not self.warned_bad_stream_start:
                source.warning(r"stream keyword terminated by \r without \n")
                self.private.warned_bad_stream_start = True
        return startstream

    def readstream(self, obj, startstream, source,
                   streamending='endstream endobj'.split(), int=int):
        fdata = source.fdata
        length = int(obj.Length)
        source.floc = target_endstream = startstream + length
        endit = source.multiple(2)
        obj._stream = fdata[startstream:target_endstream]
        if endit == streamending:
            return

        # The length attribute does not match the distance between the
        # stream and endstream keywords.

        do_warn, self.warned_bad_stream_end = self.warned_bad_stream_end, False

        # TODO:  Extract maxstream from dictionary of object offsets
        # and use rfind instead of find.
        maxstream = len(fdata) - 20
        endstream = fdata.find('endstream', startstream, maxstream)
        source.floc = startstream
        room = endstream - startstream
        if endstream < 0:
            source.error('Could not find endstream')
            return
        if length == room + 1 and fdata[startstream - 2:startstream] == '\r\n':
            source.warning(r"stream keyword terminated by \r without \n")
            obj._stream = fdata[startstream - 1:target_endstream - 1]
            return
        source.floc = endstream
        if length > room:
            source.error(('stream /Length attribute (%d) appears to be too big'
                         ' (size %d) -- adjusting'), length, room)
            obj.stream = fdata[startstream:endstream]
            return
        if fdata[target_endstream:endstream].rstrip():
            source.error(('stream /Length attribute (%d) might be smaller'
                          ' than data size (%d)'), length, room)
            return
        endobj = fdata.find('endobj', endstream, maxstream)
        if endobj < 0:
            source.error('Could not find endobj after endstream')
            return
        if fdata[endstream:endobj].rstrip() != 'endstream':
            source.error('Unexpected data between endstream and endobj')
            return
        source.error('Illegal endstream/endobj combination')

    def loadindirect(self, key):
        result = self.indirect_objects.get(key)
        if not isinstance(result, PdfIndirect):
            return result
        source = self.source
        offset = int(self.source.obj_offsets.get(key, '0'))
        if not offset:
            log.warning("Did not find PDF object %s" % (key,))
            return None

        # Read the object header and validate it
        objnum, gennum = key
        source.floc = offset
        objid = source.multiple(3)
        ok = len(objid) == 3
        ok = ok and objid[0].isdigit() and int(objid[0]) == objnum
        ok = ok and objid[1].isdigit() and int(objid[1]) == gennum
        ok = ok and objid[2] == 'obj'
        if not ok:
            source.floc = offset
            source.next()
            objheader = '%d %d obj' % (objnum, gennum)
            fdata = source.fdata
            offset2 = (fdata.find('\n' + objheader) + 1 or
                       fdata.find('\r' + objheader) + 1)
            if (not offset2 or
                    fdata.find(fdata[offset2 - 1] + objheader, offset2) > 0):
                source.warning("Expected indirect object '%s'" % objheader)
                return None
            source.warning(("Indirect object %s found at incorrect" +
                            "offset %d (expected offset %d)") %
                           (objheader, offset2, offset))
            source.floc = offset2 + len(objheader)

        # Read the object, and call special code if it starts
        # an array or dictionary
        obj = source.next()
        func = self.special.get(obj)
        if func is not None:
            obj = func(source)

        self.indirect_objects[key] = obj
        self.deferred_objects.remove(key)

        # Mark the object as indirect, and
        # add it to the list of streams if it starts a stream
        obj.indirect = key
        tok = source.next()
        if tok != 'endobj':
            self.readstream(obj, self.findstream(obj, tok, source), source)
        return obj

    def findxref(fdata):
        ''' Find the cross reference section at the end of a file
        '''
        startloc = fdata.rfind('startxref')
        if startloc < 0:
            raise PdfParseError('Did not find "startxref" at end of file')
        source = PdfTokens(fdata, startloc, False)
        tok = source.next()
        assert tok == 'startxref'  # (We just checked this...)
        tableloc = source.next_default()
        if not tableloc.isdigit():
            source.exception('Expected table location')
        if source.next_default().rstrip().lstrip('%') != 'EOF':
            source.exception('Expected %%EOF')
        return startloc, PdfTokens(fdata, int(tableloc), True)
    findxref = staticmethod(findxref)

    def parsexref(self, source, int=int, range=range):
        ''' Parse (one of) the cross-reference file section(s)
        '''

        def _pairs(array):
            i = 0
            while 1:
                yield int(array[i]), int(array[i + 1])
                i += 2
                if (i + 1) >= len(array):
                    break

        def convert_to_int(d, size):
            if size > 8:
                source.exception('Invalid size in convert_to_int')
            d = '\x00\x00\x00\x00\x00\x00\x00\x00' + d
            d = d[-8:]
            return struct.unpack('>q', d)[0]

        def read_trailer():
            tok = next()
            if tok != '<<':
                source.exception('Expected "<<" starting catalog')
            return self.readdict(source)

        setdefault = source.obj_offsets.setdefault
        add_offset = source.all_offsets.append
        next = source.next
        tok = next()
        if tok.isdigit():
            # check for xref stream object
            objid = source.multiple(2)
            ok = len(objid) == 2
            ok = ok and objid[0].isdigit()
            ok = ok and objid[1] == 'obj'
            if ok:
                next()  # start of dict
                obj = self.readdict(source)
                assert obj.Type == '/XRef'
                tok = next()
                end = source.floc + int(obj.Length)
                self.readstream(obj, self.findstream(obj, tok, source), source)
                uncompress([obj])
                num_pairs = obj.Index or PdfArray(['0', obj.Size])
                entry_sizes = [int(x) for x in obj.W]
                object_streams = {}
                for num, size in _pairs(num_pairs):
                    cnt = 0
                    stream_offset = 0
                    while cnt < size:
                        for i in range(len(entry_sizes)):
                            d = obj.stream[stream_offset:stream_offset +
                                                         entry_sizes[i]]
                            stream_offset += entry_sizes[i]
                            di = convert_to_int(d, entry_sizes[i])
                            if i == 0:
                                xref_type = di
                                if xref_type == 0 and entry_sizes[0] == 0:
                                    xref_type = 1
                            elif i == 1:
                                if xref_type == 1:
                                    offset = di
                                elif xref_type == 2:
                                    objnum = di
                            elif i == 2:
                                if xref_type == 1:
                                    generation = di
                                elif xref_type == 2:
                                    obstr_idx = di
                        if xref_type == 1 and offset != 0:
                            setdefault((num, generation), offset)
                            add_offset(offset)
                        elif xref_type == 2:
                            if not objnum in object_streams:
                                object_streams[objnum] = []
                            object_streams[objnum].append(obstr_idx)
                        cnt += 1
                        num += 1

                self.load_stream_objects(object_streams)

                source.floc = end
                endit = source.multiple(2)
                if endit != ['endstream', 'endobj']:
                    source.exception('Expected endstream endobj')
                return obj
            else:
                source.exception('Expected xref stream')

        elif tok == 'xref':
            # plain xref table
            start = source.floc
            try:
                while 1:
                    tok = next()
                    if tok == 'trailer':
                        return read_trailer()
                    startobj = int(tok)
                    for objnum in range(startobj, startobj + int(next())):
                        offset = int(next())
                        generation = int(next())
                        inuse = next()
                        if inuse == 'n':
                            if offset != 0:
                                setdefault((objnum, generation), offset)
                                add_offset(offset)
                        elif inuse != 'f':
                            raise ValueError
            except:
                pass
            try:
                # Table formatted incorrectly.
                # See if we can figure it out anyway.
                end = source.fdata.rindex('trailer', start)
                table = source.fdata[start:end].splitlines()
                for line in table:
                    tokens = line.split()
                    if len(tokens) == 2:
                        objnum = int(tokens[0])
                    elif len(tokens) == 3:
                        offset, generation, inuse = \
                            int(tokens[0]), int(tokens[1]), tokens[2]
                        if offset != 0 and inuse == 'n':
                            setdefault((objnum, generation), offset)
                            add_offset(offset)
                        objnum += 1
                    elif tokens:
                        log.error('Invalid line in xref table: %s' %
                                  repr(line))
                        raise ValueError
                log.warning('Badly formatted xref table')
                source.floc = end
                next()
            except:
                source.floc = start
                source.exception('Invalid table format')

            return read_trailer()
        else:
            source.exception('Expected "xref" keyword or xref stream object')

    def readpages(self, node):
        pagename = PdfName.Page
        pagesname = PdfName.Pages
        catalogname = PdfName.Catalog
        typename = PdfName.Type
        kidname = PdfName.Kids

        # PDFs can have arbitrarily nested Pages/Page
        # dictionary structures.
        def readnode(node):
            nodetype = node[typename]
            if nodetype == pagename:
                yield node
            elif nodetype == pagesname:
                for node in node[kidname]:
                    for node in readnode(node):
                        yield node
            elif nodetype == catalogname:
                for node in readnode(node[pagesname]):
                    yield node
            else:
                log.error('Expected /Page or /Pages dictionary, got %s' %
                    repr(node))
        try:
            return list(readnode(node))
        except (AttributeError, TypeError), s:
            log.error('Invalid page tree: %s' % s)
            return []

    def __init__(self, fname=None, fdata=None, decompress=False,
                 disable_gc=True):

        # Runs a lot faster with GC off.
        disable_gc = disable_gc and gc.isenabled()
        try:
            if disable_gc:
                gc.disable()
            if fname is not None:
                assert fdata is None
                # Allow reading preexisting streams like pyPdf
                if hasattr(fname, 'read'):
                    fdata = fname.read()
                else:
                    try:
                        f = open(fname, 'rb')
                        fdata = f.read()
                        f.close()
                    except IOError:
                        raise PdfParseError('Could not read PDF file %s' %
                            fname)

            assert fdata is not None
            if not fdata.startswith('%PDF-'):
                startloc = fdata.find('%PDF-')
                if startloc >= 0:
                    log.warning('PDF header not at beginning of file')
                else:
                    lines = fdata.lstrip().splitlines()
                    if not lines:
                        raise PdfParseError('Empty PDF file!')
                    raise PdfParseError('Invalid PDF header: %s' %
                        repr(lines[0]))

            self.version = fdata[5:8]

            endloc = fdata.rfind('%EOF')
            if endloc < 0:
                raise PdfParseError('EOF mark not found: %s' %
                    repr(fdata[-20:]))
            endloc += 6
            junk = fdata[endloc:]
            fdata = fdata[:endloc]
            if junk.rstrip('\00').strip():
                log.warning('Extra data at end of file')

            private = self.private
            private.indirect_objects = {}
            private.deferred_objects = set()
            private.special = {'<<': self.readdict,
                               '[': self.readarray,
                               'endobj': self.empty_obj,
                               }
            for tok in r'\ ( ) < > { } ] >> %'.split():
                self.special[tok] = self.badtoken

            startloc, source = self.findxref(fdata)
            private.source = source
            xref_list = []
            source.all_offsets = []
            while 1:
                source.obj_offsets = {}

                # Loop through all the cross-reference tables/streams
                trailer = self.parsexref(source)

                # Loop if any previously-written xrefs.
                prev = trailer.Prev
                if prev is None:
                    token = source.next()
                    if token != 'startxref':
                        source.warning('Expected "startxref" at end of xref table')
                    break
                if not xref_list:
                    trailer.Prev = None
                    original_trailer = trailer
                source.floc = int(prev)
                xref_list.append(source.obj_offsets)

            if xref_list:
                for update in reversed(xref_list):
                    source.obj_offsets.update(update)
                trailer.update(original_trailer)

            if trailer.Version and \
                    float(trailer.Version) > float(self.version):
                self.version = trailer.Version

            trailer = PdfDict(
                Root=trailer.Root,
                Info=trailer.Info,
                ID=trailer.ID
                # TODO: add Encrypt when implemented
            )
            self.update(trailer)

            #self.read_all_indirect(source)
            private.pages = self.readpages(self.Root)
            if decompress:
                self.uncompress()
        finally:
            if disable_gc:
                gc.enable()

    def load_stream_objects(self, object_streams):
        # read object streams
        objs = []
        for num in object_streams.iterkeys():
            obj = self.findindirect(num, 0).real_value()
            assert obj.Type == '/ObjStm'
            objs.append(obj)

        # read objects from stream
        if objs:
            uncompress(objs)
            for obj in objs:
                objsource = PdfTokens(obj.stream, 0, False)
                snext = objsource.next
                offsets = {}
                firstoffset = int(obj.First)
                num = snext()
                while num.isdigit():
                    offset = int(snext())
                    offsets[int(num)] = firstoffset + offset
                    num = snext()
                for num, offset in offsets.iteritems():
                    # Read the object, and call special code if it starts
                    # an array or dictionary
                    objsource.floc = offset
                    sobj = snext()
                    func = self.special.get(sobj)
                    if func is not None:
                        sobj = func(objsource)

                    key = (num, 0)
                    self.indirect_objects[key] = sobj
                    if key in self.deferred_objects:
                        self.deferred_objects.remove(key)

                    # Mark the object as indirect, and
                    # add it to the list of streams if it starts a stream
                    sobj.indirect = key

    def read_all(self):
        deferred = self.deferred_objects
        prev = set()
        while 1:
            new = deferred - prev
            if not new:
                break
            prev |= deferred
            for key in new:
                self.loadindirect(key)

    def read_all(self):
        deferred = self.deferred_objects
        prev = set()
        while 1:
            new = deferred - prev
            if not new:
                break
            prev |= deferred
            for key in new:
                self.loadindirect(key)

    def uncompress(self):
        self.read_all()
        uncompress(self.indirect_objects.itervalues())
