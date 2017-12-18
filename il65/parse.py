"""
Intermediate Language for 6502/6510 microprocessors
This is the parser of the IL65 code, that generates a parse tree.

Written by Irmen de Jong (irmen@razorvine.net)
License: GNU GPL 3.0, see LICENSE
"""

import sys
import re
import os
import shutil
import enum
from typing import Set, List, Tuple, Optional, Any, Dict
from astparse import ParseError, parse_expr_as_int, parse_expr_as_number, parse_expr_as_primitive,\
    parse_expr_as_string
from symbols import SymbolTable, DataType, SymbolDefinition, SubroutineDef, \
    zeropage, check_value_in_range, coerce_value, char_to_bytevalue, \
    VariableDef, ConstantDef, SymbolError, STRING_DATATYPES, \
    REGISTER_SYMBOLS, REGISTER_WORDS, REGISTER_BYTES, RESERVED_NAMES


class ProgramFormat(enum.Enum):
    PRG = "prg"
    RAW = "raw"


class ParseResult:
    def __init__(self, sourcefile: str) -> None:
        self.format = ProgramFormat.RAW
        self.with_sys = False
        self.sourcefile = sourcefile
        self.clobberzp = False
        self.restorezp = False
        self.start_address = 0
        self.blocks = []          # type: List['ParseResult.Block']

    class Block:
        _unnamed_block_labels = {}  # type: Dict[ParseResult.Block, str]

        def __init__(self, name: str, sourcefile: str, linenum: int, parent_scope: SymbolTable) -> None:
            self.sourcefile = sourcefile
            self.linenum = linenum
            self.address = 0
            self.name = name
            self.statements = []    # type: List[ParseResult._Stmt]
            self.symbols = SymbolTable(name, parent_scope, self)

        @property
        def label_names(self) -> Set[str]:
            return {symbol.name for symbol in self.symbols.iter_labels()}

        @property
        def label(self) -> str:
            if self.name:
                return self.name
            if self in self._unnamed_block_labels:
                return self._unnamed_block_labels[self]
            label = "il65_block_{:d}".format(len(self._unnamed_block_labels))
            self._unnamed_block_labels[self] = label
            return label

        def lookup(self, dottedname: str) -> Tuple[Optional['ParseResult.Block'], Optional[SymbolDefinition]]:
            try:
                scope, result = self.symbols.lookup(dottedname)
                return scope.owning_block, result
            except (SymbolError, LookupError):
                return None, None

    class Value:
        def __init__(self, datatype: DataType, name: str=None, constant: bool=False) -> None:
            self.datatype = datatype
            self.name = name
            self.constant = constant

        def assignable_from(self, other: 'ParseResult.Value') -> Tuple[bool, str]:
            if self.constant:
                return False, "cannot assign to a constant"
            return False, "incompatible value for assignment"

    class PlaceholderSymbol(Value):
        def assignable_from(self, other: 'ParseResult.Value') -> Tuple[bool, str]:
            return True, ""

        def __str__(self):
            return "<Placeholder unresolved {:s}>".format(self.name)

    class IntegerValue(Value):
        def __init__(self, value: Optional[int], *, datatype: DataType=None, name: str=None) -> None:
            if type(value) is int:
                if datatype is None:
                    if 0 <= value < 0x100:
                        datatype = DataType.BYTE
                    elif value < 0x10000:
                        datatype = DataType.WORD
                    else:
                        raise OverflowError("value too big: ${:x}".format(value))
                else:
                    faultreason = check_value_in_range(datatype, "", 1, value)
                    if faultreason:
                        raise OverflowError(faultreason)
                super().__init__(datatype, name, True)
                self.value = value
            elif value is None:
                if not name:
                    raise ValueError("when integer value is not given, the name symbol should be speicified")
                super().__init__(datatype, name, True)
                self.value = None
            else:
                raise TypeError("invalid data type")

        def __hash__(self):
            return hash((self.datatype, self.value, self.name))

        def __eq__(self, other: Any) -> bool:
            if not isinstance(other, ParseResult.IntegerValue):
                return NotImplemented
            elif self is other:
                return True
            else:
                return other.datatype == self.datatype and other.value == self.value and other.name == self.name

        def __str__(self):
            return "<IntegerValue {} name={}>".format(self.value, self.name)

    class FloatValue(Value):
        def __init__(self, value: float, name: str=None) -> None:
            if type(value) is float:
                super().__init__(DataType.FLOAT, name, True)
                self.value = value
            else:
                raise TypeError("invalid data type")

        def __hash__(self):
            return hash((self.datatype, self.value, self.name))

        def __eq__(self, other: Any) -> bool:
            if not isinstance(other, ParseResult.FloatValue):
                return NotImplemented
            elif self is other:
                return True
            else:
                return other.datatype == self.datatype and other.value == self.value and other.name == self.name

        def __str__(self):
            return "<FloatValue {} name={}>".format(self.value, self.name)

    class StringValue(Value):
        def __init__(self, value: str, name: str=None, constant: bool=False) -> None:
            super().__init__(DataType.STRING, name, constant)
            self.value = value

        def __hash__(self):
            return hash((self.datatype, self.value, self.name))

        def __eq__(self, other: Any) -> bool:
            if not isinstance(other, ParseResult.StringValue):
                return NotImplemented
            elif self is other:
                return True
            else:
                return other.datatype == self.datatype and other.value == self.value and other.name == self.name

        def __str__(self):
            return "<StringValue {!r:s} name={} constant={}>".format(self.value, self.name, self.constant)

    class RegisterValue(Value):
        def __init__(self, register: str, datatype: DataType, name: str=None) -> None:
            assert datatype in (DataType.BYTE, DataType.WORD)
            assert register in REGISTER_SYMBOLS
            super().__init__(datatype, name, False)
            self.register = register

        def __hash__(self):
            return hash((self.datatype, self.register, self.name))

        def __eq__(self, other: Any) -> bool:
            if not isinstance(other, ParseResult.RegisterValue):
                return NotImplemented
            elif self is other:
                return True
            else:
                return other.datatype == self.datatype and other.register == self.register and other.name == self.name

        def __str__(self):
            return "<RegisterValue {:s} type {:s} name={}>".format(self.register, self.datatype, self.name)

        def assignable_from(self, other: 'ParseResult.Value') -> Tuple[bool, str]:
            if self.constant:
                return False, "cannot assign to a constant"
            if isinstance(other, ParseResult.RegisterValue) and len(self.register) != len(other.register):
                return False, "register size mismatch"
            if isinstance(other, ParseResult.StringValue) and self.register in REGISTER_BYTES:
                return False, "string address requires 16 bits combined register"
            if isinstance(other, (ParseResult.IntegerValue, ParseResult.FloatValue)):
                range_error = check_value_in_range(self.datatype, self.register, 1, other.value)
                if range_error:
                    return False, range_error
                return True, ""
            if isinstance(other, ParseResult.PlaceholderSymbol):
                return True, ""
            if self.datatype == DataType.BYTE:
                if other.datatype != DataType.BYTE:
                    return False, "(unsigned) byte required"
                return True, ""
            if self.datatype == DataType.WORD:
                if other.datatype in (DataType.BYTE, DataType.WORD) or other.datatype in STRING_DATATYPES:
                    return True, ""
                return False, "(unsigned) byte, word or string required"
            return False, "incompatible value for assignment"

    class MemMappedValue(Value):
        def __init__(self, address: Optional[int], datatype: DataType, length: int, name: str=None, constant: bool=False) -> None:
            super().__init__(datatype, name, constant)
            self.address = address
            self.length = length

        def __hash__(self):
            return hash((self.datatype, self.address, self.length, self.name))

        def __eq__(self, other: Any) -> bool:
            if not isinstance(other, ParseResult.MemMappedValue):
                return NotImplemented
            elif self is other:
                return True
            else:
                return other.datatype == self.datatype and other.address == self.address and \
                       other.length == self.length and other.name == self.name

        def __str__(self):
            addr = "" if self.address is None else "${:04x}".format(self.address)
            return "<MemMappedValue {:s} type={:s} #={:d} name={} constant={}>"\
                .format(addr, self.datatype, self.length, self.name, self.constant)

        def assignable_from(self, other: 'ParseResult.Value') -> Tuple[bool, str]:
            if self.constant:
                return False, "cannot assign to a constant"
            if isinstance(other, ParseResult.PlaceholderSymbol):
                return True, ""
            if self.datatype == DataType.BYTE:
                if isinstance(other, (ParseResult.IntegerValue, ParseResult.RegisterValue)):
                    if other.datatype == DataType.BYTE:
                        return True, ""
                    return False, "can't assign non-byte to byte"
                elif isinstance(other, ParseResult.FloatValue):
                    range_error = check_value_in_range(self.datatype, "", 1, other.value)
                    if range_error:
                        return False, range_error
                    return True, ""
                else:
                    return False, "(unsigned) byte required"
            elif self.datatype in (DataType.WORD, DataType.FLOAT):
                if isinstance(other, (ParseResult.IntegerValue, ParseResult.FloatValue)):
                    range_error = check_value_in_range(self.datatype, "", 1, other.value)
                    if range_error:
                        return False, range_error
                    return True, ""
                elif isinstance(other, ParseResult.RegisterValue):
                    if other.datatype == DataType.BYTE:
                        if self.datatype in (DataType.BYTE, DataType.WORD, DataType.FLOAT):
                            return True, ""
                        return False, "can't assign register to this"
                    elif other.datatype == DataType.WORD:
                        if self.datatype in (DataType.WORD, DataType.FLOAT):
                            return True, ""
                        return False, "can't assign 16 bit combined registers to byte"
                elif isinstance(other, ParseResult.StringValue):
                    if self.datatype == DataType.WORD:
                        return True, ""
                    return False, "string address requires 16 bits (a word)"
                if self.datatype == DataType.BYTE:
                    return False, "(unsigned) byte required"
                if self.datatype == DataType.WORD:
                    return False, "(unsigned) word required"
            return False, "incompatible value for assignment"

    class _Stmt:
        def resolve_symbol_references(self, parser: 'Parser', cur_block: 'ParseResult.Block') -> None:
            pass

    class Label(_Stmt):
        def __init__(self, name: str, linenum: int) -> None:
            self.name = name
            self.linenum = linenum

    class AssignmentStmt(_Stmt):
        def __init__(self, leftvalues: List['ParseResult.Value'], right: 'ParseResult.Value', linenum: int) -> None:
            self.leftvalues = leftvalues
            self.right = right
            self.linenum = linenum

        def __str__(self):
            return "<Assign {:s} to {:s}>".format(str(self.right), ",".join(str(lv) for lv in self.leftvalues))

        def resolve_symbol_references(self, parser: 'Parser', cur_block: 'ParseResult.Block') -> None:
            if isinstance(self.right, ParseResult.PlaceholderSymbol):
                value = parser.parse_expression(self.right.name, cur_block)
                if isinstance(value, ParseResult.PlaceholderSymbol):
                    raise ParseError("cannot resolve rvalue symbol: " + self.right.name, "", cur_block.sourcefile, cur_block.linenum)
                self.right = value
            lv_resolved = []
            for lv in self.leftvalues:
                if isinstance(lv, ParseResult.PlaceholderSymbol):
                    value = parser.parse_expression(lv.name, cur_block)
                    if isinstance(value, ParseResult.PlaceholderSymbol):
                        raise ParseError("cannot resolve lvalue symbol: " + lv.name, "", cur_block.sourcefile, cur_block.linenum)
                    lv_resolved.append(value)
                else:
                    lv_resolved.append(lv)
            self.leftvalues = lv_resolved
            if any(isinstance(lv, ParseResult.PlaceholderSymbol) for lv in self.leftvalues) or \
                    isinstance(self.right, ParseResult.PlaceholderSymbol):
                raise ParseError("unresolved placeholders in assignment statement", "", cur_block.sourcefile, cur_block.linenum)
            # check assignability again
            for lv in self.leftvalues:
                assignable, reason = lv.assignable_from(self.right)
                if not assignable:
                    raise ParseError("cannot assign {0} to {1}; {2}".format(self.right, lv, reason), "",
                                     cur_block.sourcefile, cur_block.linenum)

        _immediate_string_vars = {}   # type: Dict[str, Tuple[str, str]]

        def desugar_immediate_string(self, cur_block: 'ParseResult.Block') -> None:
            if self.right.name or not isinstance(self.right, ParseResult.StringValue):
                return
            if self.right.value in self._immediate_string_vars:
                blockname, stringvar_name = self._immediate_string_vars[self.right.value]
                if blockname:
                    self.right.name = blockname + "." + stringvar_name
                else:
                    self.right.name = stringvar_name
            else:
                stringvar_name = "il65_str_{:d}".format(id(self))
                cur_block.symbols.define_variable(stringvar_name, cur_block.sourcefile, 0, DataType.STRING, value=self.right.value)
                self.right.name = stringvar_name
                self._immediate_string_vars[self.right.value] = (cur_block.name, stringvar_name)

    class ReturnStmt(_Stmt):
        def __init__(self, a: Optional['ParseResult.Value']=None,
                     x: Optional['ParseResult.Value']=None,
                     y: Optional['ParseResult.Value']=None) -> None:
            self.a = a
            self.x = x
            self.y = y

        def resolve_symbol_references(self, parser: 'Parser', cur_block: 'ParseResult.Block') -> None:
            if isinstance(self.a, ParseResult.PlaceholderSymbol) or \
               isinstance(self.x, ParseResult.PlaceholderSymbol) or \
               isinstance(self.y, ParseResult.PlaceholderSymbol):
                raise ParseError("unresolved placeholders in return statement", "", cur_block.sourcefile, cur_block.linenum)

    class IncrDecrStmt(_Stmt):
        def __init__(self, what: 'ParseResult.Value', howmuch: int) -> None:
            self.what = what
            self.howmuch = howmuch

        def resolve_symbol_references(self, parser: 'Parser', cur_block: 'ParseResult.Block') -> None:
            if isinstance(self.what, ParseResult.PlaceholderSymbol):
                value = parser.parse_expression(self.what.name, cur_block)
                if isinstance(value, ParseResult.PlaceholderSymbol):
                    raise ParseError("cannot resolve symbol: " + self.what.name, "", cur_block.sourcefile, cur_block.linenum)
                self.what = value

    class CallStmt(_Stmt):
        def __init__(self, line_number: int, address: Optional[int]=None, unresolved: str=None,
                     params: Dict[str, Any]=None, is_goto: bool=False, preserve_regs: bool=True) -> None:
            self.address = address
            self.subroutine = None      # type: SubroutineDef
            self.unresolved = unresolved
            self.is_goto = is_goto
            self.preserve_regs = preserve_regs
            self.call_module = ""
            self.call_label = ""
            self.line_number = line_number
            self.params = params or {}

        def resolve_symbol_references(self, parser: 'Parser', cur_block: 'ParseResult.Block') -> None:
            if self.unresolved:
                symblock, identifier = cur_block.lookup(self.unresolved)
                if not identifier:
                    raise ParseError("unknown symbol '{:s}'".format(self.unresolved), "", cur_block.sourcefile, self.line_number)
                if isinstance(identifier, SubroutineDef):
                    self.subroutine = identifier
                if cur_block is symblock:
                    self.call_module, self.call_label = "", identifier.name
                else:
                    self.call_module = symblock.label
                self.call_label = identifier.name
                self.unresolved = None

    class InlineAsm(_Stmt):
        def __init__(self, linenum: int, asmlines: List[str]) -> None:
            self.linenum = linenum
            self.asmlines = asmlines

    def add_block(self, block: 'ParseResult.Block', position: Optional[int]=None) -> None:
        if position is not None:
            self.blocks.insert(position, block)
        else:
            self.blocks.append(block)

    def merge(self, parsed: 'ParseResult') -> None:
        self.blocks.extend(parsed.blocks)


class Parser:
    def __init__(self, sourcefile: str, outputdir: str, sourcecode: Optional[str]=None, parsing_import: bool=False) -> None:
        self.result = ParseResult(sourcefile)
        self.sourcefile = sourcefile
        self.outputdir = outputdir
        self.parsing_import = parsing_import     # are we parsing a import file?
        self.cur_linenum = -1
        self.cur_lineidx = -1
        self.cur_block = None  # type: ParseResult.Block
        self.root_scope = SymbolTable("<root>", None, None)
        if sourcecode:
            sourcelines = sourcecode.splitlines()
        else:
            with open(self.sourcefile, "rU") as source:
                sourcelines = source.readlines()
        # store all lines that are not empty or a comment, and strip any other comments
        self.lines = []  # type: List[Tuple[int, str]]
        for num, line in enumerate(sourcelines, start=1):
            line2 = line.strip()
            if not line2 or line2.startswith(";"):
                continue
            self.lines.append((num, line.partition(";")[0].rstrip()))  # get rid of any comments at the end of the line

    def parse(self) -> Optional[ParseResult]:
        # start the parsing
        try:
            return self._parse()
        except ParseError as x:
            if x.text:
                print("\tsource text: '{:s}'".format(x.text))
                if x.offset:
                    print("\t" + ' '*x.offset + '             ^')
            if self.parsing_import:
                print("Error (in imported file):", str(x))
            else:
                print("Error:", str(x))
            raise   # XXX temporary solution to get stack trace info in the event of parse errors
        except Exception as x:
            print("ERROR: internal parser error: ", x)
            print("    file:", self.sourcefile, "block:", self.cur_block.name, "line:", self.cur_linenum)
            raise   # XXX temporary solution to get stack trace info in the event of parse errors

    def _parse(self) -> ParseResult:
        print("\nparsing (pass 1)", self.sourcefile)
        self.parse_header()
        zeropage.configure(self.result.clobberzp)
        while True:
            next_line = self.peek_next_line()[1]
            if next_line.lstrip().startswith("~"):
                block = self.parse_block()
                if block:
                    self.result.add_block(block)
            elif next_line.lstrip().startswith("import"):
                self.parse_import()
            else:
                break
        _, line = self.next_line()
        if line:
            raise self.PError("invalid statement or characters, block expected")
        if not self.parsing_import:
            # check if we have a proper main block to contain the program's entry point
            for block in self.result.blocks:
                if block.name == "main":
                    if "start" not in block.label_names:
                        self.cur_linenum = block.linenum
                        raise self.PError("The 'main' block should contain the program entry point 'start'")
                    if not any(s for s in block.statements if isinstance(s, ParseResult.ReturnStmt)):
                        print("warning: {:s}:{:d}: The 'main' block is lacking a return statement.".format(self.sourcefile, block.linenum))
                    break
            else:
                raise self.PError("A block named 'main' should be defined for the program's entry point 'start'")
        # parsing pass 2
        print("\nparsing (pass 2)", self.sourcefile)
        # fix up labels that are unknown, and desugar immediate string value assignments:
        for block in self.result.blocks:
            statements = list(block.statements)
            for index, stmt in enumerate(statements):
                stmt.resolve_symbol_references(self, block)
                if isinstance(stmt, ParseResult.AssignmentStmt):
                    stmt.desugar_immediate_string(block)
            block.statements = statements
        # done parsing.
        return self.result

    def next_line(self) -> Tuple[int, str]:
        self.cur_lineidx += 1
        try:
            self.cur_linenum, line = self.lines[self.cur_lineidx]
            return self.cur_linenum, line
        except IndexError:
            return -1, ""

    def prev_line(self) -> Tuple[int, str]:
        self.cur_lineidx -= 1
        self.cur_linenum, line = self.lines[self.cur_lineidx]
        return self.cur_linenum, line

    def peek_next_line(self) -> Tuple[int, str]:
        if (self.cur_lineidx + 1) < len(self.lines):
            return self.lines[self.cur_lineidx + 1]
        return -1, ""

    def PError(self, message: str) -> ParseError:
        try:
            sourceline = self.lines[self.cur_lineidx][1].strip()
        except IndexError:
            sourceline = ""
        return ParseError(message, sourceline, self.sourcefile, self.cur_linenum)

    def parse_header(self) -> None:
        self.result.with_sys = False
        self.result.format = ProgramFormat.RAW
        output_specified = False
        while True:
            num, line = self.next_line()
            if line.startswith("output"):
                if output_specified:
                    raise self.PError("multiple occurrences of 'output'")
                output_specified = True
                _, _, arg = line.partition(" ")
                arg = arg.lstrip()
                self.result.with_sys = False
                self.result.format = ProgramFormat.RAW
                if arg == "raw":
                    pass
                elif arg == "prg":
                    self.result.format = ProgramFormat.PRG
                elif arg.replace(' ', '') == "prg,sys":
                    self.result.with_sys = True
                    self.result.format = ProgramFormat.PRG
                else:
                    raise self.PError("invalid output format")
            elif line.startswith("clobberzp"):
                if self.result.clobberzp:
                    raise self.PError("multiple occurrences of 'clobberzp'")
                self.result.clobberzp = True
                _, _, arg = line.partition(" ")
                arg = arg.lstrip()
                if arg == "restore":
                    self.result.restorezp = True
                elif arg == "":
                    pass
                else:
                    raise self.PError("invalid arg for clobberzp")
            elif line.startswith("address"):
                if self.result.start_address:
                    raise self.PError("multiple occurrences of 'address'")
                _, _, arg = line.partition(" ")
                try:
                    self.result.start_address = parse_expr_as_int(arg, None, self.sourcefile, self.cur_linenum)
                except ParseError:
                    raise self.PError("invalid address")
                if self.result.format == ProgramFormat.PRG and self.result.with_sys and self.result.start_address != 0x0801:
                    raise self.PError("cannot use non-default 'address' when output format includes basic SYS program")
            else:
                # header parsing finished!
                self.prev_line()
                if not self.result.start_address:
                    # set the proper default start address
                    if self.result.format == ProgramFormat.PRG:
                        self.result.start_address = 0x0801  # normal C-64 basic program start address
                    elif self.result.format == ProgramFormat.RAW:
                        self.result.start_address = 0xc000  # default start for raw assembly
                if self.result.format == ProgramFormat.PRG and self.result.with_sys and self.result.start_address != 0x0801:
                    raise self.PError("cannot use non-default 'address' when output format includes basic SYS program")
                return

    def parse_import(self) -> None:
        num, line = self.next_line()
        line = line.lstrip()
        if not line.startswith("import"):
            raise self.PError("expected import")
        try:
            _, arg = line.split(maxsplit=1)
        except ValueError:
            raise self.PError("invalid import statement")
        if not arg.startswith('"') or not arg.endswith('"'):
            raise self.PError("filename must be between quotes")
        filename = arg[1:-1]
        if not filename:
            raise self.PError("invalid filename")
        filename_at_source_location = os.path.join(os.path.split(self.sourcefile)[0], filename)
        filename_at_libs_location = os.path.join(os.path.split(sys.argv[0])[0], "lib", filename)
        candidates = [filename,
                      filename_at_source_location,
                      filename_at_libs_location,
                      filename+".ill",
                      filename_at_source_location+".ill",
                      filename_at_libs_location+".ill"]
        for filename in candidates:
            if os.path.isfile(filename):
                parser = Parser(filename, self.outputdir, parsing_import=True)
                print("importing", filename)
                result = parser.parse()
                print("\ncontinuing", self.sourcefile)
                if result:
                    self.root_scope.merge_roots(parser.root_scope)
                    self.result.merge(result)
                    return
                else:
                    raise self.PError("Error while parsing imported file")
        raise self.PError("imported file not found")

    def parse_block(self) -> ParseResult.Block:
        # first line contains block header "~ [name] [addr]" followed by a '{'
        linenum, line = self.next_line()
        line = line.lstrip()
        if not line.startswith("~"):
            raise self.PError("expected '~' (block)")
        block_args = line[1:].split()
        arg = ""
        self.cur_block = ParseResult.Block("", self.sourcefile, linenum, self.root_scope)
        is_zp_block = False
        while block_args:
            arg = block_args.pop(0)
            if arg.isidentifier():
                if arg.lower() == "zeropage" or arg in ("zp", "zP", "Zp"):
                    raise self.PError("zero page block should be named 'ZP'")
                is_zp_block = arg == "ZP"
                if arg in set(b.name for b in self.result.blocks):
                    orig = [b for b in self.result.blocks if b.name == arg][0]
                    if not is_zp_block:
                        raise self.PError("duplicate block name '{0:s}', original definition at {1:s} line {2:d}"
                                          .format(arg, orig.sourcefile, orig.linenum))
                    self.cur_block = orig  # zero page block occurrences are merged
                else:
                    self.cur_block = ParseResult.Block(arg, self.sourcefile, linenum, self.root_scope)
                    try:
                        self.root_scope.define_scope(self.cur_block.symbols)
                    except SymbolError as x:
                        raise self.PError(str(x))
            elif arg == "{":
                break
            elif arg.endswith("{"):
                # when there is no whitespace before the {
                block_args.insert(0, "{")
                block_args.insert(0, arg[:-1])
                continue
            else:
                try:
                    block_address = parse_expr_as_int(arg, None, self.sourcefile, self.cur_linenum)
                except ParseError:
                    raise self.PError("Invalid block address")
                if block_address == 0 or (block_address < 0x0200 and not is_zp_block):
                    raise self.PError("block address must be >= $0200 (or omitted)")
                if is_zp_block:
                    if block_address not in (0, 0x04):
                        raise self.PError("zero page block address must be $04 (or omittted)")
                    block_address = 0x04
                self.cur_block.address = block_address
        if arg != "{":
            _, line = self.peek_next_line()
            if line != "{":
                raise self.PError("expected '{' after block")
            else:
                self.next_line()
        if self.cur_block.address:
            print("  parsing block '{:s}' at ${:04x}".format(self.cur_block.name, self.cur_block.address))
        else:
            print("  parsing block '{:s}'".format(self.cur_block.name))
        while True:
            _, line = self.next_line()
            unstripped_line = line
            line = line.strip()
            if line == "}":
                if is_zp_block and any(b.name == "ZP" for b in self.result.blocks):
                    return None     # we already have the ZP block
                if not self.cur_block.name and not self.cur_block.address:
                    print("warning: {:s}:{:d}: Ignoring block without name and address."
                          .format(self.sourcefile, self.cur_block.linenum))
                    return None
                return self.cur_block
            if line.startswith("var"):
                self.parse_var_def(line)
            elif line.startswith("const"):
                self.parse_const_def(line)
            elif line.startswith("memory"):
                self.parse_memory_def(line, is_zp_block)
            elif line.startswith("subx"):
                if is_zp_block:
                    raise self.PError("ZP block cannot contain subroutines")
                self.parse_subx_def(line)
            elif line.startswith(("asminclude", "asmbinary")):
                if is_zp_block:
                    raise self.PError("ZP block cannot contain assembler directives")
                self.cur_block.statements.append(self.parse_asminclude(line))
            elif line.startswith("asm"):
                if is_zp_block:
                    raise self.PError("ZP block cannot contain code statements")
                self.prev_line()
                self.cur_block.statements.append(self.parse_asm())
                continue
            elif unstripped_line.startswith((" ", "\t")):
                if is_zp_block:
                    raise self.PError("ZP block cannot contain code statements")
                self.cur_block.statements.append(self.parse_statement(line))
                continue
            elif line:
                if is_zp_block:
                    raise self.PError("ZP block cannot contain code labels")
                self.parse_label(line)
            else:
                raise self.PError("missing } to close block from line " + str(self.cur_block.linenum))

    def parse_label(self, line: str) -> None:
        label_line = line.split(maxsplit=1)
        if str.isidentifier(label_line[0]):
            labelname = label_line[0]
            if labelname in self.cur_block.label_names:
                raise self.PError("label already defined")
            if labelname in self.cur_block.symbols:
                raise self.PError("symbol already defined")
            self.cur_block.symbols.define_label(labelname, self.sourcefile, self.cur_linenum)
            self.cur_block.statements.append(ParseResult.Label(labelname, self.cur_linenum))
            if len(label_line) > 1:
                rest = label_line[1]
                self.cur_block.statements.append(self.parse_statement(rest))
        else:
            raise self.PError("invalid label name")

    def parse_memory_def(self, line: str, is_zeropage: bool=False) -> None:
        varname, datatype, length, dimensions, valuetext = self.parse_def_common(line, "memory")
        memaddress = parse_expr_as_int(valuetext, self.cur_block.symbols, self.sourcefile, self.cur_linenum)
        if is_zeropage and memaddress > 0xff:
            raise self.PError("address must lie in zeropage $00-$ff")
        try:
            self.cur_block.symbols.define_variable(varname, self.sourcefile, self.cur_linenum, datatype,
                                                   length=length, address=memaddress, matrixsize=dimensions)
        except SymbolError as x:
            raise self.PError(str(x)) from x

    def parse_const_def(self, line: str) -> None:
        varname, datatype, length, dimensions, valuetext = self.parse_def_common(line, "const")
        if dimensions:
            raise self.PError("cannot declare a constant matrix")
        value = parse_expr_as_primitive(valuetext, self.cur_block.symbols, self.sourcefile, self.cur_linenum)
        _, value = coerce_value(self.sourcefile, self.cur_linenum, datatype, value)
        try:
            self.cur_block.symbols.define_constant(varname, self.sourcefile, self.cur_linenum, datatype,
                                                   length=length, value=value)
        except (ValueError, SymbolError) as x:
            raise self.PError(str(x)) from x

    def parse_subx_def(self, line: str) -> None:
        match = re.match(r"^subx\s+(?P<name>\w+)\s+"
                         r"\((?P<parameters>[\w\s:,]*)\)"
                         r"\s*->\s*"
                         r"\((?P<results>[\w\s?,]*)\)\s*"
                         r"\s+=\s+(?P<address>\S*)\s*$", line)
        if not match:
            raise self.PError("invalid subx declaration")
        name, parameterlist, resultlist, address_str = \
            match.group("name"), match.group("parameters"), match.group("results"), match.group("address")
        parameters = [(match.group("name"), match.group("target"))
                      for match in re.finditer(r"(?:(?:(?P<name>[\w]+)\s*:\s*)?(?P<target>[\w]+))(?:,|$)", parameterlist)]
        for _, regs in parameters:
            if regs not in REGISTER_SYMBOLS:
                raise self.PError("invalid register(s) in parameter or return values")
        all_paramnames = [p[0] for p in parameters if p[0]]
        if len(all_paramnames) != len(set(all_paramnames)):
            raise self.PError("duplicates in parameter names")
        results = {match.group("name") for match in re.finditer(r"\s*(?P<name>(?:\w+)\??)\s*(?:,|$)", resultlist)}
        try:
            address = parse_expr_as_int(address_str, None, self.sourcefile, self.cur_linenum)
        except ParseError:
            raise self.PError("invalid subroutine address")
        try:
            self.cur_block.symbols.define_sub(name, self.sourcefile, self.cur_linenum, parameters, results, address)
        except SymbolError as x:
            raise self.PError(str(x)) from x

    def get_datatype(self, typestr: str) -> Tuple[DataType, int, Optional[Tuple[int, int]]]:
        if typestr == ".byte":
            return DataType.BYTE, 1, None
        elif typestr == ".word":
            return DataType.WORD, 1, None
        elif typestr == ".float":
            return DataType.FLOAT, 1, None
        elif typestr.endswith("text"):
            if typestr == ".text":
                return DataType.STRING, 0, None
            elif typestr == ".ptext":
                return DataType.STRING_P, 0, None
            elif typestr == ".stext":
                return DataType.STRING_S, 0, None
            elif typestr == ".pstext":
                return DataType.STRING_PS, 0, None
        elif typestr.startswith(".array(") and typestr.endswith(")"):
            return DataType.BYTEARRAY, self._size_from_arraydecl(typestr), None
        elif typestr.startswith(".wordarray(") and typestr.endswith(")"):
            return DataType.WORDARRAY, self._size_from_arraydecl(typestr), None
        elif typestr.startswith(".matrix(") and typestr.endswith(")"):
            dimensions = self._size_from_matrixdecl(typestr)
            return DataType.MATRIX, dimensions[0] * dimensions[1], dimensions
        raise self.PError("invalid data type: " + typestr)

    def parse_var_def(self, line: str) -> None:
        varname, datatype, length, dimensions, valuetext = self.parse_def_common(line, "var", False)
        value = parse_expr_as_primitive(valuetext, self.cur_block.symbols, self.sourcefile, self.cur_linenum)
        _, value = coerce_value(self.sourcefile, self.cur_linenum, datatype, value)
        try:
            self.cur_block.symbols.define_variable(varname, self.sourcefile, self.cur_linenum, datatype,
                                                   length=length, value=value, matrixsize=dimensions)
        except (ValueError, SymbolError) as x:
            raise self.PError(str(x)) from x

    def parse_def_common(self, line: str, what: str, value_required: bool=True) -> \
            Tuple[str, DataType, int, Optional[Tuple[int, int]], str]:
        try:
            vartext, valuetext = line.split("=", maxsplit=1)
        except ValueError:
            if '=' not in line:
                if value_required:
                    raise self.PError("missing value assignment")
                vartext, valuetext = line, "0"  # unspecified value is '0'
            else:
                raise self.PError("invalid {:s} decl, '=' missing?".format(what))
        args = self.psplit(vartext)
        if args[0] != what or len(args) < 2:
            raise self.PError("invalid {:s} decl".format(what))
        if len(args) > 3 or valuetext.startswith('='):
            raise self.PError("invalid {:s} decl, '=' missing?".format(what))
        if len(args) == 2:
            args.insert(1, ".byte")  # unspecified data type is ".byte"
        if not args[1].startswith("."):
            raise self.PError("invalid {:s} decl, type is missing".format(what))
        varname = args[2]
        if not varname.isidentifier():
            raise self.PError("invalid {:s} name".format(what))
        if varname in RESERVED_NAMES:
            raise self.PError("can't use a reserved name as {:s} name".format(what))
        datatype, length, matrix_dimensions = self.get_datatype(args[1])
        return varname, datatype, length, matrix_dimensions, valuetext

    def parse_statement(self, line: str) -> ParseResult._Stmt:
        # check if we have a subroutine call using () syntax
        match = re.match(r"^(?P<subname>[\w\.]+)\s*(?P<fcall>[!]?)\s*\((?P<params>.*)\)\s*$", line)
        if match:
            subname = match.group("subname")
            fcall = "f" if match.group("fcall") else ""
            param_str = match.group("params")
            # desugar this into "[f]call subname parameters"
            line = "{:s}call {:s} {:s}".format(fcall, subname, param_str)
        if line.startswith("return"):
            return self.parse_return(line)
        elif line.endswith(("++", "--")):
            incr = line.endswith("++")
            what = self.parse_expression(line[:-2].rstrip())
            if isinstance(what, ParseResult.IntegerValue):
                raise self.PError("cannot in/decrement a constant value")
            return ParseResult.IncrDecrStmt(what, 1 if incr else -1)
        elif line.startswith("call"):
            return self.parse_call_or_go(line, "call")
        elif line.startswith("fcall"):
            return self.parse_call_or_go(line, "fcall")
        elif line.startswith("go"):
            return self.parse_call_or_go(line, "go")
        else:
            # perhaps it is an assignment statment
            lhs, sep, rhs = line.partition("=")
            if sep:
                return self.parse_assignment(line)
            raise self.PError("invalid statement")

    def parse_call_or_go(self, line: str, what: str) -> ParseResult.CallStmt:
        args = line.split(maxsplit=2)
        if len(args) == 2:
            subname, params, = args[1], ""
            parameters = None
        elif len(args) == 3:
            subname, params = args[1], args[2]
            parameters = {match.group("pname"): match.group("value")
                          for match in re.finditer(r"(?:(?:(?P<pname>[\w]+)\s*=\s*)(?P<value>.+?))(?:,|$)", params)}
        else:
            raise self.PError("invalid call/go arguments")
        if what == "go":
            return ParseResult.CallStmt(self.cur_linenum, unresolved=subname, is_goto=True)
        elif what == "call":
            return ParseResult.CallStmt(self.cur_linenum, unresolved=subname, params=parameters)
        elif what == "fcall":
            return ParseResult.CallStmt(self.cur_linenum, unresolved=subname, params=parameters, preserve_regs=False)
        else:
            raise ValueError("invalid what")

    def parse_assignment(self, line: str) -> ParseResult.AssignmentStmt:
        # parses assigning a value to one or more targets
        parts = line.split("=")
        rhs = parts.pop()
        l_values = [self.parse_expression(part) for part in parts]
        if any(isinstance(lv, ParseResult.IntegerValue) for lv in l_values):
            raise self.PError("can't have a constant as assignment target, did you mean [name] instead?")
        r_value = self.parse_expression(rhs)
        for lv in l_values:
            assignable, reason = lv.assignable_from(r_value)
            if not assignable:
                raise self.PError("cannot assign {0} to {1}; {2}".format(r_value, lv, reason))
            if lv.datatype in (DataType.BYTE, DataType.WORD, DataType.MATRIX):
                if isinstance(r_value, ParseResult.FloatValue):
                    truncated, value = coerce_value(self.sourcefile, self.cur_linenum, lv.datatype, r_value.value)
                    if truncated:
                        r_value = ParseResult.IntegerValue(int(value), datatype=lv.datatype, name=r_value.name)
        return ParseResult.AssignmentStmt(l_values, r_value, self.cur_linenum)

    def parse_return(self, line: str) -> ParseResult.ReturnStmt:
        parts = line.split(maxsplit=1)
        if parts[0] != "return":
            raise self.PError("invalid statement, return expected")
        a = x = y = None
        values = []  # type: List[str]
        if len(parts) > 1:
            values = parts[1].split(",")
        if len(values) == 0:
            return ParseResult.ReturnStmt()
        else:
            a = self.parse_expression(values[0]) if values[0] else None
            if len(values) > 1:
                x = self.parse_expression(values[1]) if values[1] else None
                if len(values) > 2:
                    y = self.parse_expression(values[2]) if values[2] else None
                    if len(values) > 3:
                        raise self.PError("too many returnvalues")
        return ParseResult.ReturnStmt(a, x, y)

    def parse_asm(self) -> ParseResult.InlineAsm:
        asm_line_num, line = self.next_line()
        aline = line.split()
        if not len(aline) == 2 or aline[0] != "asm" or aline[1] != "{":
            raise self.PError("invalid asm start")
        asmlines = []   # type: List[str]
        while True:
            num, line = self.next_line()
            if line.strip() == "}":
                return ParseResult.InlineAsm(asm_line_num, asmlines)
            asmlines.append(line)

    def parse_asminclude(self, line: str) -> ParseResult.InlineAsm:
        aline = line.split()
        if len(aline) < 2:
            raise self.PError("invalid asminclude or asmbinary statement")
        filename = aline[1]
        if not filename.startswith('"') or not filename.endswith('"'):
            raise self.PError("filename must be between quotes")
        filename = filename[1:-1]
        if not filename:
            raise self.PError("invalid filename")
        filename_in_sourcedir = os.path.join(os.path.split(self.sourcefile)[0], filename)
        filename_in_output_location = os.path.join(self.outputdir, filename)
        if not os.path.isfile(filename_in_sourcedir):
            raise self.PError("included file not found")
        print("copying included file to output location:", filename)
        shutil.copy(filename_in_sourcedir, filename_in_output_location)
        if aline[0] == "asminclude":
            if len(aline) == 3:
                scopename = aline[2]
                lines = ['{:s}\t.binclude "{:s}"'.format(scopename, filename)]
            else:
                raise self.PError("invalid asminclude statement")
            return ParseResult.InlineAsm(self.cur_linenum, lines)
        elif aline[0] == "asmbinary":
            if len(aline) == 4:
                offset = parse_expr_as_int(aline[2], None, self.sourcefile, self.cur_linenum)
                length = parse_expr_as_int(aline[3], None, self.sourcefile, self.cur_linenum)
                lines = ['\t.binary "{:s}", ${:04x}, ${:04x}'.format(filename, offset, length)]
            elif len(aline) == 3:
                offset = parse_expr_as_int(aline[2], None, self.sourcefile, self.cur_linenum)
                lines = ['\t.binary "{:s}", ${:04x}'.format(filename, offset)]
            elif len(aline) == 2:
                lines = ['\t.binary "{:s}"'.format(filename)]
            else:
                raise self.PError("invalid asmbinary statement")
            return ParseResult.InlineAsm(self.cur_linenum, lines)
        else:
            raise self.PError("invalid statement")

    def parse_expression(self, text: str, cur_block: Optional[ParseResult.Block]=None) -> ParseResult.Value:
        # parse an expression into whatever it is (primitive value, register, memory, register, etc)
        cur_block = cur_block or self.cur_block
        text = text.strip()
        if not text:
            raise self.PError("value expected")
        if text[0] == '#':
            # take the pointer (memory address) from the thing that follows this
            expression = self.parse_expression(text[1:], cur_block)
            if isinstance(expression, ParseResult.StringValue):
                return expression
            elif isinstance(expression, ParseResult.MemMappedValue):
                return ParseResult.IntegerValue(expression.address, datatype=DataType.WORD, name=expression.name)
            elif isinstance(expression, ParseResult.PlaceholderSymbol):
                print("EX", expression)
                raise self.PError("cannot take the address from an unresolved symbol")
            else:
                raise self.PError("cannot take the address from this type")
        elif text[0] in "-.0123456789$%":
            number = parse_expr_as_number(text, None, self.sourcefile, self.cur_linenum)
            try:
                if type(number) is int:
                    return ParseResult.IntegerValue(int(number))
                elif type(number) is float:
                    return ParseResult.FloatValue(number)
                else:
                    raise TypeError("invalid number type")
            except (ValueError, OverflowError) as ex:
                raise self.PError(str(ex))
        elif text in REGISTER_WORDS:
            return ParseResult.RegisterValue(text, DataType.WORD)
        elif text in REGISTER_BYTES:
            return ParseResult.RegisterValue(text, DataType.BYTE)
        elif (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
            strvalue = parse_expr_as_string(text, None, self.sourcefile, self.cur_linenum)
            if len(strvalue) == 1:
                petscii_code = char_to_bytevalue(strvalue)
                return ParseResult.IntegerValue(petscii_code)
            return ParseResult.StringValue(strvalue)
        elif text == "true":
            return ParseResult.IntegerValue(1)
        elif text == "false":
            return ParseResult.IntegerValue(0)
        elif self.is_identifier(text):
            symblock, sym = cur_block.lookup(text)
            if sym is None:
                # symbols is not (yet) known, store a placeholder to resolve later in parse pass 2
                return ParseResult.PlaceholderSymbol(None, text)
            elif isinstance(sym, (VariableDef, ConstantDef)):
                constant = isinstance(sym, ConstantDef)
                if cur_block is symblock:
                    symbolname = sym.name
                else:
                    symbolname = "{:s}.{:s}".format(sym.blockname, sym.name)
                if isinstance(sym, VariableDef) and sym.register:
                    return ParseResult.RegisterValue(sym.register, sym.type, name=symbolname)
                elif sym.type in (DataType.BYTE, DataType.WORD, DataType.FLOAT):
                    if isinstance(sym, ConstantDef):
                        symbolvalue = sym.value
                    else:
                        symbolvalue = sym.address
                    return ParseResult.MemMappedValue(symbolvalue, sym.type, sym.length, name=symbolname, constant=constant)  # type:ignore
                elif sym.type in STRING_DATATYPES:
                    return ParseResult.StringValue(sym.value, name=symbolname, constant=constant)      # type: ignore
                elif sym.type == DataType.MATRIX:
                    raise self.PError("cannot manipulate matrix directly, use one of the matrix procedures")
                elif sym.type == DataType.BYTEARRAY or sym.type == DataType.WORDARRAY:
                    raise self.PError("cannot manipulate array directly, use one of the array procedures")
                else:
                    raise self.PError("invalid symbol type (1)")
            else:
                raise self.PError("invalid symbol type (2)")
        elif text.startswith('[') and text.endswith(']'):
            num_or_name = text[1:-1].strip()
            word_type = float_type = False
            if num_or_name.endswith(".word"):
                word_type = True
                num_or_name = num_or_name[:-5]
            elif num_or_name.endswith(".float"):
                float_type = True
                num_or_name = num_or_name[:-6]
            if num_or_name.isidentifier():
                try:
                    sym = cur_block.symbols[num_or_name]    # type: ignore
                except KeyError:
                    raise self.PError("unknown symbol (2): " + num_or_name)
                if isinstance(sym, ConstantDef):
                    if sym.type == DataType.BYTE and (word_type or float_type):
                        raise self.PError("byte value required")
                    elif sym.type == DataType.WORD and float_type:
                        raise self.PError("word value required")
                    if type(sym.value) is int:
                        return ParseResult.MemMappedValue(int(sym.value), sym.type, sym.length, sym.name)
                    else:
                        raise TypeError("integer required")
                elif isinstance(sym, VariableDef):
                    if sym.type == DataType.BYTE and (word_type or float_type):
                        raise self.PError("byte value required")
                    elif sym.type == DataType.WORD and float_type:
                        raise self.PError("word value required")
                    return ParseResult.MemMappedValue(sym.address, sym.type, sym.length, sym.name)
                else:
                    raise self.PError("invalid symbol type used as lvalue of assignment (3)")
            else:
                addr = parse_expr_as_int(num_or_name, None, self.sourcefile, self.cur_linenum)
                if word_type:
                    return ParseResult.MemMappedValue(addr, DataType.WORD, length=1)
                elif float_type:
                    return ParseResult.MemMappedValue(addr, DataType.FLOAT, length=1)
                else:
                    return ParseResult.MemMappedValue(addr, DataType.BYTE, length=1)
        else:
            raise self.PError("invalid value '" + text + "'")

    def is_identifier(self, name: str) -> bool:
        if name.isidentifier():
            return True
        blockname, sep, name = name.partition(".")
        if sep:
            return blockname.isidentifier() and name.isidentifier()
        return False

    def _size_from_arraydecl(self, decl: str) -> int:
        return parse_expr_as_int(decl[:-1].split("(")[-1], self.cur_block.symbols, self.sourcefile, self.cur_linenum)

    def _size_from_matrixdecl(self, decl: str) -> Tuple[int, int]:
        dimensions = decl[:-1].split("(")[-1]
        try:
            xs, ys = dimensions.split(",")
        except ValueError:
            raise self.PError("invalid matrix dimensions")
        return (parse_expr_as_int(xs, self.cur_block.symbols, self.sourcefile, self.cur_linenum),
                parse_expr_as_int(ys, self.cur_block.symbols, self.sourcefile, self.cur_linenum))

    def psplit(self, sentence: str, separators: str=" \t", lparen: str="(", rparen: str=")") -> List[str]:
        """split a sentence but not on separators within parenthesis"""
        nb_brackets = 0
        sentence = sentence.strip(separators)  # get rid of leading/trailing seps
        indices = [0]
        for i, c in enumerate(sentence):
            if c == lparen:
                nb_brackets += 1
            elif c == rparen:
                nb_brackets -= 1
            elif c in separators and nb_brackets == 0:
                indices.append(i)
            # handle malformed string
            if nb_brackets < 0:
                raise self.PError("syntax error")

        indices.append(len(sentence))
        # handle missing closing parentheses
        if nb_brackets > 0:
            raise self.PError("syntax error")
        result = [sentence[i:j].strip(separators) for i, j in zip(indices, indices[1:])]
        return list(filter(None, result))   # remove empty strings


class Optimizer:
    def __init__(self, parseresult: ParseResult) -> None:
        self.parsed = parseresult

    def optimize(self) -> ParseResult:
        print("\noptimizing parse tree...")
        for block in self.parsed.blocks:
            self.combine_assignments_into_multi(block)
            self.optimize_multiassigns(block)
        return self.parsed

    def optimize_multiassigns(self, block: ParseResult.Block) -> None:
        # optimize multi-assign statements.
        for stmt in block.statements:
            if isinstance(stmt, ParseResult.AssignmentStmt) and len(stmt.leftvalues) > 1:
                # remove duplicates
                lvalues = list(set(stmt.leftvalues))
                if len(lvalues) != len(stmt.leftvalues):
                    print("{:s}:{:d} removed duplicate assignment targets".format(block.sourcefile, stmt.linenum))
                # change order: first registers, then zp addresses, then non-zp addresses, then the rest (if any)
                stmt.leftvalues = list(sorted(lvalues, key=value_sortkey))

    def combine_assignments_into_multi(self, block: ParseResult.Block) -> None:
        # fold multiple consecutive assignments with the same rvalue into one multi-assignment
        statements = []   # type: List[ParseResult._Stmt]
        multi_assign_statement = None
        for stmt in block.statements:
            if isinstance(stmt, ParseResult.AssignmentStmt):
                if multi_assign_statement and multi_assign_statement.right == stmt.right:
                    multi_assign_statement.leftvalues.extend(stmt.leftvalues)
                    print("{:s}:{:d} joined with previous line into multi-assign statement".format(block.sourcefile, stmt.linenum))
                else:
                    if multi_assign_statement:
                        statements.append(multi_assign_statement)
                    multi_assign_statement = stmt
            else:
                if multi_assign_statement:
                    statements.append(multi_assign_statement)
                    multi_assign_statement = None
                statements.append(stmt)
        if multi_assign_statement:
            statements.append(multi_assign_statement)
        block.statements = statements


def value_sortkey(value: ParseResult.Value) -> int:
    if isinstance(value, ParseResult.RegisterValue):
        num = 0
        for char in value.register:
            num *= 100
            num += ord(char)
        return num
    elif isinstance(value, ParseResult.MemMappedValue):
        if value.address < 0x100:
            return 10000 + value.address
        else:
            return 20000 + value.address
    else:
        return 99999999


if __name__ == "__main__":
    p = Parser("parse.py", outputdir="output")
    p.cur_block = ParseResult.Block("test", "testfile", 1, None)
    p.parse_subx_def("subx  SUBNAME   (A, test2:XY, X) -> (A?, X) = $c000")
    sub = list(p.cur_block.symbols.iter_subroutines())[0]
    import pprint
    pprint.pprint(vars(sub))
