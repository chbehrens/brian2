import itertools

import numpy as np

from brian2.parsing.rendering import NumpyNodeRenderer
from brian2.core.functions import DEFAULT_FUNCTIONS, Function
from brian2.core.variables import ArrayVariable
from brian2.utils.stringtools import get_identifiers, word_substitute, indent
from brian2.utils.logger import get_logger

from .base import CodeGenerator

__all__ = ['NumpyCodeGenerator']


logger = get_logger(__name__)

class VectorisationError(Exception):
    pass


class NumpyCodeGenerator(CodeGenerator):
    '''
    Numpy language
    
    Essentially Python but vectorised.
    '''

    class_name = 'numpy'

    def translate_expression(self, expr):
        for varname, var in self.variables.iteritems():
            if isinstance(var, Function):
                impl_name = var.implementations[self.codeobj_class].name
                if impl_name is not None:
                    expr = word_substitute(expr, {varname: impl_name})
        return NumpyNodeRenderer().render_expr(expr, self.variables).strip()

    def translate_statement(self, statement):
        # TODO: optimisation, translate arithmetic to a sequence of inplace
        # operations like a=b+c -> add(b, c, a)
        var, op, expr, comment = (statement.var, statement.op,
                                  statement.expr, statement.comment)
        if op == ':=':
            op = '='
        code = var + ' ' + op + ' ' + self.translate_expression(expr)
        if len(comment):
            code += ' # ' + comment
        return code

    def ufunc_at_vectorisation(self, statement, variables, indices,
                               conditional_write_vars, created_vars, index):
        '''
        '''
        # Avoids circular import
        from brian2.devices.device import device

        # We assume that the code has passed the test for synapse order independence

        main_index_variables = [v for v in variables if indices[v] == index]

        lines = []
        need_unique_indices = set()

        vars_in_expr = get_identifiers(statement.expr).intersection(variables)
        subs = {}
        # for var in vars_in_expr:
        #     if not isinstance(var, ArrayVariable):
        #         continue
        #     subs[var] = '{var}[{idx}]'.format(var=var, idx=indices[var])
        # expr = word_substitute(statement.expr, subs)
        expr = NumpyNodeRenderer().render_expr(statement.expr)
        if statement.var in main_index_variables or statement.op == ':=':
            if statement.op in (':=', '='):
                op = '='
            else:
                op = statement.op
            line = '{var} {op} {expr}'.format(var=statement.var,
                                              op=op,
                                              expr=expr)

        else:
            if statement.inplace:
                if statement.op=='+=':
                    ufunc_name = '_numpy.add'
                elif statement.op=='*=':
                    ufunc_name = '_numpy.multiply'
                else:
                    raise VectorisationError()
                line = '{ufunc_name}.at({array_name}, {idx}, {expr})'.format(ufunc_name=ufunc_name,
                                                                            array_name=device.get_array_name(variables[statement.var]),
                                                                            idx=indices[statement.var],
                                                                            expr=expr)
                line = self.conditional_write(line, statement, variables,
                                              conditional_write_vars=conditional_write_vars,
                                              created_vars=created_vars)
            else:
                # if statement is not in-place then we assume the expr has no synaptic
                # variables in it otherwise it would have failed the order independence
                # check. In this case, we only need to work with the unique indices

                # TODO: This is more complicated since it needs to change the
                # indexing/read/write. Note sure it is worth the effort, it's
                # only an optimisation isn't it?

                # need_unique_indices.add(indices[statement.var])
                # idx = '_unique_' + indices[statement.var]
                # expr = word_substitute(expr, {indices[statement.var]: idx})

                line = '{var} = {expr}'.format(var=statement.var,
                                               expr=expr)

        if len(statement.comment):
            line += ' # ' + statement.comment
        lines.append(line)

        for unique_idx in need_unique_indices:
            lines.insert(0, '_unique_{idx} = _numpy.unique({idx})'.format(idx=unique_idx))

        return lines

    def vectorise_code(self, statements, variables, variable_indices, index='_idx'):

        # We treat every statement individually with its own read and write code
        # to be on the safe side
        lines = []
        created_vars = {stmt.var for stmt in statements if stmt.op == ':='}
        for statement in statements:
            lines.append('#  Abstract code:  {var} {op} {expr}'.format(var=statement.var,
                                                                       op=statement.op,
                                                                       expr=statement.expr))
            read, write, indices, conditional_write_vars = self.arrays_helper([statement])
            try:
                # We make sure that we only add code to `lines` after it went
                # through completely
                ufunc_lines = []
                ufunc_lines.extend(self.read_arrays(read, write, indices,
                                              variables, variable_indices))
                ufunc_lines.extend(self.ufunc_at_vectorisation(statement,
                                                               variables,
                                                               variable_indices,
                                                               conditional_write_vars,
                                                               created_vars,
                                                               index=index))
                # Do not write back such values, the ufuncs have modified the
                # underlying array already
                if statement.op in ('+=', '*='):
                    write -= {statement.var}
                ufunc_lines.extend(self.write_arrays([statement], read, write,
                                                     variables,
                                                     variable_indices))
                lines.extend(ufunc_lines)
            except VectorisationError:
                logger.warn("Failed to vectorise synapses code, falling back on Python loop: note that "
                            "this will be very slow! Switch to another code generation target for "
                            "best performance (e.g. cython or weave).",
                            once=True)
                lines.extend(['_full_idx = _idx',
                              'for _idx in _full_idx:'])
                lines.extend(indent(code) for code in
                             self.read_arrays(read, write, indices,
                                              variables, variable_indices))
                line = self.translate_statement(statement)
                line = self.conditional_write(line, statement, variables,
                                              conditional_write_vars,
                                              created_vars)
                lines.append(indent(line))
                lines.extend(indent(code) for code in
                             self.write_arrays(statements, read, write,
                                               variables, variable_indices))
                lines.append('_idx = _full_idx')

        return lines

    def read_arrays(self, read, write, indices, variables, variable_indices):
        # index and read arrays (index arrays first)
        lines = []
        for varname in itertools.chain(indices, read):
            var = variables[varname]
            index = variable_indices[varname]
            # if index in iterate_all:
            #                line = '{varname} = {array_name}'
            #            else:
            #                line = '{varname} = {array_name}.take({index})'
            #            line = line.format(varname=varname, array_name=self.get_array_name(var), index=index)
            line = varname + ' = ' + self.get_array_name(var)
            if not index in self.iterate_all:
                line += '[' + index + ']'
            elif varname in write:
                # avoid potential issues with aliased variables, see github #259
                line += '.copy()'
            lines.append(line)
        return lines

    def write_arrays(self, statements, read, write, variables, variable_indices):
        # write arrays
        lines = []
        for varname in write:
            var = variables[varname]
            index_var = variable_indices[varname]
            # check if all operations were inplace and we're operating on the
            # whole vector, if so we don't need to write the array back
            if index_var not in self.iterate_all or varname in read:
                all_inplace = False
            else:
                all_inplace = True
                for stmt in statements:
                    if stmt.var == varname and not stmt.inplace:
                        all_inplace = False
                        break
            if not all_inplace:
                line = self.get_array_name(var)
                if index_var in self.iterate_all:
                    line = line + '[:]'
                else:
                    line = line + '[' + index_var + ']'
                line = line + ' = ' + varname
                lines.append(line)
        return lines

    def conditional_write(self, line, stmt, variables, conditional_write_vars,
                          created_vars):
        if stmt.var in conditional_write_vars:
            subs = {}
            index = conditional_write_vars[stmt.var]
            # we replace all var with var[index], but actually we use this repl_string first because
            # we don't want to end up with lines like x[not_refractory[not_refractory]] when
            # multiple substitution passes are invoked
            repl_string = '#$(@#&$@$*U#@)$@(#'  # this string shouldn't occur anywhere I hope! :)
            for varname, var in variables.items():
                if isinstance(var, ArrayVariable):
                    subs[varname] = varname + '[' + repl_string + ']'
            # all newly created vars are arrays and will need indexing
            for varname in created_vars:
                subs[varname] = varname + '[' + repl_string + ']'
            line = word_substitute(line, subs)
            line = line.replace(repl_string, index)
        return line

    def translate_one_statement_sequence(self, statements, scalar=False):
        variables = self.variables
        variable_indices = self.variable_indices
        read, write, indices, conditional_write_vars = self.arrays_helper(statements)
        lines = []

        # Check whether we potentially deal with repeated indices (which will
        # be the case most importantly when we write to pre- or post-synaptic
        # variables in synaptic code)
        used_indices = set(variable_indices[var] for var in write)
        all_unique = all(variables[index].unique for index in used_indices
                         if index not in ('_idx', '0'))

        if scalar or all_unique:
            # Simple translation
            lines.extend(self.read_arrays(read, write, indices, variables,
                                          variable_indices))
            created_vars = {stmt.var for stmt in statements if stmt.op == ':='}
            for stmt in statements:

                line = self.translate_statement(stmt)
                line = self.conditional_write(line, stmt, variables,
                                              conditional_write_vars,
                                              created_vars)
                lines.append(line)
            lines.extend(self.write_arrays(statements, read, write, variables,
                                           variable_indices))
        else:
            # More complex translation to deal with repeated indices
            lines.extend(self.vectorise_code(statements, variables,
                                             variable_indices))

        # Make sure we do not use the __call__ function of Function objects but
        # rather the Python function stored internally. The __call__ function
        # would otherwise return values with units
        for varname, var in variables.iteritems():
            if isinstance(var, Function):
                variables[varname] = var.implementations[self.codeobj_class].get_code(self.owner)

        return lines

    def determine_keywords(self):
        # For numpy, no addiional keywords are provided to the template
        return {}

################################################################################
# Implement functions
################################################################################
# Functions that exist under the same name in numpy
for func_name, func in [('sin', np.sin), ('cos', np.cos), ('tan', np.tan),
                        ('sinh', np.sinh), ('cosh', np.cosh), ('tanh', np.tanh),
                        ('exp', np.exp), ('log', np.log), ('log10', np.log10),
                        ('sqrt', np.sqrt), ('arcsin', np.arcsin),
                        ('arccos', np.arccos), ('arctan', np.arctan),
                        ('abs', np.abs), ('mod', np.fmod)]:
    DEFAULT_FUNCTIONS[func_name].implementations.add_implementation(NumpyCodeGenerator,
                                                                    code=func)

# Functions that are implemented in a somewhat special way
def randn_func(vectorisation_idx):
    try:
        N = len(vectorisation_idx)
    except TypeError:
        N = int(vectorisation_idx)

    numbers = np.random.randn(N)
    if N == 1:
        return numbers[0]
    else:
        return numbers


def rand_func(vectorisation_idx):
    try:
        N = len(vectorisation_idx)
    except TypeError:
        N = int(vectorisation_idx)

    numbers = np.random.rand(N)
    if N == 1:
        return numbers[0]
    else:
        return numbers


DEFAULT_FUNCTIONS['randn'].implementations.add_implementation(NumpyCodeGenerator,
                                                              code=randn_func)
DEFAULT_FUNCTIONS['rand'].implementations.add_implementation(NumpyCodeGenerator,
                                                             code=rand_func)
clip_func = lambda array, a_min, a_max: np.clip(array, a_min, a_max)
DEFAULT_FUNCTIONS['clip'].implementations.add_implementation(NumpyCodeGenerator,
                                                             code=clip_func)
int_func = lambda value: np.int32(value)
DEFAULT_FUNCTIONS['int'].implementations.add_implementation(NumpyCodeGenerator,
                                                            code=int_func)
ceil_func = lambda value: np.int32(np.ceil(value))
DEFAULT_FUNCTIONS['ceil'].implementations.add_implementation(NumpyCodeGenerator,
                                                            code=ceil_func)
floor_func = lambda value: np.int32(np.floor(value))
DEFAULT_FUNCTIONS['floor'].implementations.add_implementation(NumpyCodeGenerator,
                                                            code=floor_func)
