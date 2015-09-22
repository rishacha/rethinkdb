#!/usr/bin/env python
# -*- encoding: utf-8
'''Finds yaml tests, converts them to Java tests.'''
from __future__ import print_function

import sys
import os
import os.path
import re
import time
import ast
import yaml
import argparse
import metajava
import process_polyglot
from process_polyglot import Unhandled, Skip, SkippedTest
try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO
from collections import namedtuple

# Supplied by import_python_driver
r = None


TEST_EXCLUSIONS = [
    # python only tests
    'regression/1133',
    'regression/767',
    'regression/1005',
    # double run
    'changefeeds/squash',
    # arity checked at compile time
    'arity',
]


def main():
    start = time.clock()
    args = parse_args()
    global r
    r = import_python_driver(args.test_common_dir)
    renderer = metajava.Renderer(
        args.template_dir,
        invoking_filenames=[
            __file__,
            process_polyglot.__file__,
        ])
    for testfile in process_polyglot.all_yaml_tests(
            args.test_dir,
            TEST_EXCLUSIONS):
        print("Working on", testfile)
        TestFile(
            test_dir=args.test_dir,
            filename=testfile,
            test_output_dir=args.test_output_dir,
            renderer=renderer,
        ).load().render()
    print("Finished in", time.clock() - start, "seconds")


def parse_args():
    '''Parse command line arguments'''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-dir",
        help="Directory where yaml tests are",
        default="../../test/rql_test/src"
    )
    parser.add_argument(
        "--test-output-dir",
        help="Directory to render tests to",
        default="./src/test/java/gen",
    )
    parser.add_argument(
        "--template-dir",
        help="Where to find test generation templates",
        default="./templates",
    )
    parser.add_argument(
        "--test-common-dir",
        help="Where the common test modules are located",
        default="../../test/common"
    )
    parser.add_argument(
        "--test-file",
        help="Only convert the specified yaml file",
    )
    return parser.parse_args()


def import_python_driver(common_dir):
    '''Imports the test driver header'''
    stashed_path = sys.path
    sys.path.insert(0, os.path.realpath(common_dir))
    import utils
    sys.path = stashed_path
    return utils.import_python_driver()

JavaQuery = namedtuple(
    'JavaQuery',
    ('line',
     'expected_bif',
     'expected_type',
     'expected_line',
     'testfile',
     'test_num',
     'runopts')
)
JavaDef = namedtuple('JavaDef', 'line testfile test_num')
Version = namedtuple("Version", "original java")


class TestFile(object):
    '''Represents a single test file'''

    def __init__(self, test_dir, filename, test_output_dir, renderer):
        self.filename = filename
        self.full_path = os.path.join(test_dir, filename)
        self.module_name = metajava.camel(
            filename.split('.')[0].replace('/', '_'))
        self.test_output_dir = test_output_dir
        self.reql_vars = {'r'}
        self.renderer = renderer

    def load(self):
        '''Load the test file, yaml parse it, extract file-level metadata'''
        with open(self.full_path) as f:
            parsed_yaml = yaml.load(f)
        self.description = parsed_yaml.get('desc', 'No description')
        self.table_var_names = self.get_varnames(parsed_yaml)
        self.reql_vars.update(self.table_var_names)
        self.raw_test_data = parsed_yaml['tests']
        self.test_generator = process_polyglot.tests_and_defs(
            self.filename,
            self.raw_test_data,
            process_polyglot.create_context(r, self.table_var_names)
        )
        return self

    def get_varnames(self, yaml_file):
        '''Extract table variable names from yaml variable
        They can be specified just space separated, or comma separated'''
        raw_var_names = yaml_file.get('table_variable_name', '')
        if not raw_var_names:
            return set()
        return set(re.split(r'[, ]+', raw_var_names))

    def render(self):
        '''Renders the converted tests to a runnable test file'''
        defs_and_test = ast_to_java(self.test_generator, self.reql_vars)
        self.renderer.render(
            'Test.java',
            output_dir=self.test_output_dir,
            output_name=self.module_name + '.java',
            dependencies=[self.full_path],
            defs_and_test=defs_and_test,
            table_var_names=list(sorted(self.table_var_names)),
            module_name=self.module_name,
            JavaQuery=JavaQuery,
            JavaDef=JavaDef,
        )

def py_to_java_type(py_type):
    '''Converts python types to their Java equivalents'''
    return {
        int: 'Integer',
        float: 'Double',
        str: 'String',
        dict: 'Map',
        list: 'List',
    }[py_type]


def ast_to_java(sequence, reql_vars):
    '''Converts the the parsed test data to java source lines using the
    visitor classes'''
    reql_vars = set(reql_vars)
    for item in sequence:
        if isinstance(item, process_polyglot.Def):
            if is_reql(item.term.type):
                reql_vars.add(item.varname)
            try:
                if is_reql(item.term.type):
                    visitor = ReQLVisitor(reql_vars)
                else:
                    visitor = JavaVisitor(reql_vars)
                java_line = visitor.convert(item.term.ast)
            except Skip as skip:
                yield SkippedTest(line=item.term.line, reason=str(skip))
                continue
            yield JavaDef(
                line=Version(
                    original=item.term.line,
                    java=java_line,
                ),
                testfile=item.testfile,
                test_num=item.test_num,
            )
        elif isinstance(item, process_polyglot.Query):
            if item.runopts is not None:
                converted_runopts = {
                    key: JavaVisitor(reql_vars).convert(val)
                    for key, val in item.runopts.items()
                }
            else:
                converted_runopts = item.runopts
            try:
                java_line = ReQLVisitor(reql_vars).convert(item.query.ast)
                if is_reql(item.expected.term.type):
                    visitor = ReQLVisitor(reql_vars)
                else:
                    visitor = JavaVisitor(reql_vars)
                java_expected_line = visitor.convert(item.expected.term.ast)
            except Skip as skip:
                yield SkippedTest(line=item.query.line, reason=str(skip))
                continue
            yield JavaQuery(
                line=Version(
                    original=item.query.line,
                    java=java_line,
                ),
                expected_bif=item.expected.bif,
                expected_type=py_to_java_type(item.expected.term.type),
                expected_line=Version(
                    original=item.expected.term.line,
                    java=java_expected_line,
                ),
                testfile=item.testfile,
                test_num=item.test_num,
                runopts=converted_runopts,
            )
        elif isinstance(item, SkippedTest):
            yield item
        else:
            assert False, "shouldn't happen"


class JavaVisitor(ast.NodeVisitor):
    '''Converts python ast nodes into a java string'''

    def __init__(self, reql_vars=frozenset("r"), out=None):
        self.out = StringIO() if out is None else out
        self.reql_vars = reql_vars
        super(JavaVisitor, self).__init__()
        self.write = self.out.write

    def convert(self, node):
        '''Convert a text line to another text line'''
        self.visit(node)
        return self.out.getvalue()

    def join(self, sep, items):
        first = True
        for item in items:
            if first:
                first = False
            else:
                self.write(sep)
            self.visit(item)

    def to_str(self, string):
        self.write('"')
        self.write(repr(string)[1:-1])  # trim off quotes
        self.write('"')

    def to_args(self, args, optargs=[]):
        self.write("(")
        self.join(", ", args)
        self.write(")")
        for optarg in optargs:
            self.write(".optArg(")
            self.to_str(optarg.arg)
            self.write(", ")
            self.visit(optarg.value)
            self.write(")")

    def generic_visit(self, node):
        print("While translating: " + ast.dump(node), file=sys.stderr)
        print("Got as far as:", ''.join(self.out), file=sys.stderr)
        raise Unhandled("Don't know what this thing is: " + str(type(node)))

    def visit_Assign(self, node):
        if len(node.targets) != 1:
            Unhandled("We only support assigning to one variable")
        if is_reql(node.value, self.reql_vars):
            self.write("ReqlAst ") # TODO: convert python to java type!
            self.write(node.targets[0].id)
            self.write(" = ")
            ReQLVisitor(self.reql_vars, out=self.out).visit(node.value)
            self.write(";")
        else:
            self.write("Object ")
            self.write(node.targets[0].id)
            self.write(" = ")
            self.visit(node.value)
            self.write(";")

    def visit_Str(self, node):
        self.to_str(node.s)

    def visit_Bytes(self, node):
        self.to_str(node.s)
        self.write(".getBytes(StandardCharsets.UTF_8)")

    def visit_Name(self, node):
        self.write({
            'True': 'true',
            'False': 'false',
            'None': 'null',
            'nil': 'null',
            }.get(node.id, node.id))

    def visit_arg(self, node):
        self.write(node.arg)

    def visit_NameConstant(self, node):
        if node.value is None:
            self.write("null")
        elif node.value is True:
            self.write("true")
        elif node.value is False:
            self.write("false")
        else:
            raise Unhandled(
                "Don't know NameConstant with value %s" % node.value)

    def visit_Attribute(self, node):
        self.write(".")
        self.write(node.attr)

    def visit_Num(self, node):
        self.write(repr(node.n))
        if abs(node.n) > 2147483647 and not isinstance(node.n, float):
            self.write(".0")

    def visit_Index(self, node):
        self.visit(node.value)

    def visit_Call(self, node):
        assert not node.kwargs
        assert not node.starargs
        self.visit(node.func)
        self.to_args(node.args, node.keywords)

    def visit_Dict(self, node):
        self.write("new MapObject()")
        for k, v in zip(node.keys, node.values):
            self.write(".with(")
            self.visit(k)
            self.write(", ")
            self.visit(v)
            self.write(")")

    def visit_List(self, node):
        self.write("Arrays.asList(")
        self.join(", ", node.elts)
        self.write(")")

    def visit_Tuple(self, node):
        self.visit_List(node)

    def visit_Lambda(self, node):
        if len(node.args.args) == 1:
            self.visit(node.args.args[0])
        else:
            self.to_args(node.args.args)
        self.write(" -> ")
        self.visit(node.body)

    def visit_Subscript(self, node):
        if node.slice is None or type(node.slice.value) != ast.Num:
            print("While doing:", ast.dump(node))
            raise Unhandled("Only integers subscript can be converted."
                            " Got %s" % node.slice.value.s)
        self.write("[")
        self.visit(node.slice.value)
        self.write("]")

    def visit_ListComp(self, node):
        gen = node.generators[0]

        if type(gen.iter) == ast.Call and gen.iter.func.id.endswith('range'):
            # This is really a special-case hacking of [... for i in
            # range(i)] comprehensions that are used in the polyglot
            # tests sometimes. It won't handle translating arbitrary
            # comprehensions to Java streams.
            self.write("IntStream.range(")
            if len(gen.iter.args) == 1:
                self.write("0, ")
                self.visit(gen.iter.args[0])
            elif len(gen.iter.args) == 2:
                self.visit(gen.iter.args[0])
                self.write(", ")
                self.visit(gen.iter.args[1])
            self.write(").boxed()")
        else:
            # Somebody came up with a creative new use for
            # comprehensions in the test suite...
            raise Unhandled("ListComp hack couldn't handle: ", ast.dump(node))
        self.write(".map(")
        self.visit(gen.target)
        self.write(" -> ")
        self.visit(node.elt)
        self.write(").collect(Collectors.toList())")

    def visit_UnaryOp(self, node):
        opMap = {
            ast.USub: "-",
            ast.Not: "!",
            ast.UAdd: "+",
            ast.Invert: "~",
        }
        self.write(opMap[type(node.op)])
        self.visit(node.operand)

    def visit_BinOp(self, node):
        opMap = {
            ast.Add: " + ",
            ast.Sub: " - ",
            ast.Mult: " * ",
            ast.Div: " / ",
            ast.Mod: " % ",
        }
        t = type(node.op)
        if t in opMap.keys():
            self.visit(node.left)
            self.write(opMap[t])
            self.visit(node.right)
        elif t == ast.Pow:
            self.write("Math.pow(")
            self.visit(node.left)
            self.write(", ")
            self.visit(node.right)
            self.write(")")


class ReQLVisitor(JavaVisitor):
    '''Mostly the same as the JavaVisitor, but converts some
    reql-specific stuff. This should only be invoked on an expression
    if it's already known to return true from is_reql'''

    def visit_BinOp(self, node):
        opMap = {
            ast.Add: "add",
            ast.Sub: "sub",
            ast.Mult: "mul",
            ast.Div: "div",
            ast.Mod: "mod",
            ast.BitAnd: "and",
            ast.BitOr: "or",
        }
        self.write("r.")
        self.write(opMap[type(node.op)])
        self.write("(")
        self.visit(node.left)
        self.write(", ")
        self.visit(node.right)
        self.write(")")

    def visit_Compare(self, node):
        opMap = {
            ast.Lt: "lt",
            ast.Gt: "gt",
            ast.GtE: "ge",
            ast.LtE: "le",
            ast.Eq: "eq",
            ast.NotEq: "ne",
        }
        self.write("r.")
        if len(node.ops) != 1:
            # Python syntax allows chained comparisons (a < b < c) but
            # we don't deal with that here
            raise Unhandled("Compare hack bailed on: ", ast.dump(node))
        self.write(opMap[type(node.ops[0])])
        self.write("(")
        self.visit(node.left)
        self.write(", ")
        self.visit(node.comparators[0])
        self.write(")")

    def visit_Subscript(self, node):
        self.visit(node.value)
        if type(node.slice) == ast.Index:
            # Syntax like a[2] or a["b"]
            self.write(".bracket(")
            self.visit(node.slice.value)
        elif type(node.slice) == ast.Slice:
            # Syntax like a[1:2] or a[:2]
            self.write(".slice(")
            lower, upper = self.get_slice_bounds(node.slice)
            self.write(str(lower))
            self.write(", ")
            self.write(str(upper))
        else:
            raise Unhandled("No translation for ExtSlice")
        self.write(")")

    def get_slice_bounds(self, slc):
        '''Used to extract bounds when using bracket slice
        syntax. This is more complicated since Python3 parses -1 as
        UnaryOp(op=USub, operand=Num(1)) instead of Num(-1) like
        Python2 does'''
        if not slc:
            return 0, -1

        def get_bound(bound, default):
            if bound is None:
                return default
            elif type(bound) == ast.UnaryOp and type(bound.op) == ast.USub:
                return -bound.operand.n
            elif type(bound) == ast.Num:
                return bound.n
            else:
                raise Unhandled(
                    "Not handling bound: %s" % ast.dump(bound))

        return get_bound(slc.lower, 0), get_bound(slc.upper, -1)

    def visit_Attribute(self, node):
        if node.attr == 'row' and \
           type(node.value) == ast.Name and \
           node.value.id == 'r':
            raise Skip("Java driver doesn't support r.row")
        python_clashes = {
            'or_': 'or',
            'and_': 'and',
        }
        self.visit(node.value)
        self.write(".")
        initial = python_clashes.get(
            node.attr, metajava.dromedary(node.attr))
        self.write(initial)
        if initial in metajava.java_term_info.JAVA_KEYWORDS or \
           initial in metajava.java_term_info.OBJECT_METHODS:
            self.write('_')

Bag = namedtuple("Bag", "value")
Err = namedtuple("Err", "exception message")
Partial = namedtuple("Partial", "value")
Uuid = namedtuple("Uuid", "value")
Arrlen = namedtuple("Arrlen", "array")
Repeat = namedtuple("Repeat", "rpt")


if __name__ == '__main__':
    main()