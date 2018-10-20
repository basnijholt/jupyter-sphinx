"""Simple sphinx extension that executes code in jupyter and inserts output."""

import os
from itertools import groupby, count
from operator import itemgetter

from sphinx.util import logging
from sphinx.transforms import SphinxTransform
from sphinx.errors import ExtensionError
from sphinx.addnodes import download_reference
from sphinx.ext.mathbase import displaymath

import docutils
from IPython.lib.lexers import IPythonTracebackLexer, IPython3Lexer
from docutils.parsers.rst import Directive, directives

import nbconvert
from nbconvert.preprocessors.execute import executenb
from nbconvert.preprocessors import ExtractOutputPreprocessor
from nbconvert.writers import FilesWriter

from jupyter_client.kernelspec import get_kernel_spec, NoSuchKernel

import nbformat


from ._version import __version__

logger = logging.getLogger(__name__)


### Directives and their associated doctree nodes

class JupyterKernel(Directive):
    """Specify a new Jupyter Kernel.

    Arguments
    ---------
    kernel_name : str (required)
        The name of the kernel in which to execute future Jupyter cells, as
        reported by executing 'jupyter kernelspec list' on the command line.

    Options
    -------
    id : str
        An identifier for *this kernel instance*. Used to name any output
        files generated when executing the Jupyter cells (e.g. images
        produced by cells, or a script containing the cell inputs).

    Content
    -------
    None
    """

    optional_arguments = 1
    final_argument_whitespace = False
    has_content = False

    option_spec = {
        'id': directives.unchanged,
    }

    def run(self):
        return [JupyterKernelNode(
            kernel_name=self.arguments[0] if self.arguments else '',
            kernel_id=self.options.get('id', ''),
        )]


class JupyterKernelNode(docutils.nodes.Element):

    def __init__(self, kernel_name, kernel_id):
        super().__init__(
            '',
            kernel_name=kernel_name.strip(),
            kernel_id=kernel_id.strip(),
        )


class JupyterCell(Directive):
    """Define a code cell to be later executed in a Jupyter kernel.

    The content of the directive is the code to execute. Code is not
    executed when the directive is parsed, but later during a doctree
    transformation.

    Arguments
    ---------
    filename : str (optional)
        If provided, a path to a file containing code.

    Options
    -------
    hide-code : bool
        If provided, the code will not be displayed in the output.
    hide-output : bool
        If provided, the cell output will not be displayed in the output.
    code-below : bool
        If provided, the code will be shown below the cell output.

    Content
    -------
    code : str
        A code cell.
    """

    required_arguments = 0
    optional_arguments = 1
    final_argument_whitespace = True
    has_content = True

    option_spec = {
        'hide-code': directives.flag,
        'hide-output': directives.flag,
        'code-below': directives.flag,
    }

    def run(self):
        if self.arguments:
            # As per 'sphinx.directives.code.LiteralInclude'
            env = self.state.document.settings.env
            rel_filename, filename = env.relfn2path(self.arguments[0])
            env.note_dependency(rel_filename)
            if self.content:
                logger.warning(
                    'Ignoring inline code in Jupyter cell included from "{}"'
                    .format(rel_filename)
                )
            try:
                with open(filename) as f:
                    content = f.readlines()
            except (IOError, OSError):
                raise IOError(
                    'File {} not found or reading it failed'.format(filename)
                )
        else:
            self.assert_has_content()
            content = self.content

        return [JupyterCellNode(content, self.options)]


class JupyterCellNode(docutils.nodes.container):

    def __init__(self, source_lines, options):
        return super().__init__(
            '',
            docutils.nodes.literal_block(
                text='\n'.join(source_lines),
            ),
            hide_code=('hide-code' in options),
            hide_output=('hide-output' in options),
            code_below=('code-below' in options),
        )


### Doctree transformations

class ExecuteJupyterCells(SphinxTransform):
    """Execute code cells in Jupyter kernels.

   Traverses the doctree to find JupyterKernel and JupyterCell nodes,
   then executes the code in the JupyterCell nodes in sequence, starting
   a new kernel every time a JupyterKernel node is encountered. The output
   from each code cell is inserted into the doctree.
   """
    default_priority = 180  # An early transform, idk

    def apply(self):
        doctree = self.document
        doc_relpath = os.path.dirname(self.env.docname)  # relative to src dir
        docname = os.path.basename(self.env.docname)
        default_kernel = self.config.jupyter_execute_default_kernel
        default_names = default_notebook_names(docname)

        # Check if we have anything to execute.
        if not doctree.traverse(JupyterCellNode):
            return

        logger.info('executing {}'.format(docname))
        output_dir = os.path.join(output_directory(self.env), doc_relpath)

        # Start new notebook whenever a JupyterKernelNode is encountered
        jupyter_nodes = (JupyterCellNode, JupyterKernelNode)
        nodes_by_notebook = split_on(
            lambda n: isinstance(n, JupyterKernelNode),
            doctree.traverse(lambda n: isinstance(n, jupyter_nodes))
        )

        for first, *nodes in nodes_by_notebook:
            if isinstance(first, JupyterKernelNode):
                kernel_name = first['kernel_name'] or default_kernel
                file_name = first['kernel_id'] or next(default_names)
            else:
                nodes = (first, *nodes)
                kernel_name = default_kernel
                file_name = next(default_names)

            notebook = execute_cells(
                kernel_name,
                [nbformat.v4.new_code_cell(node.astext()) for node in nodes],
                self.config.jupyter_execute_kwargs,
            )

            # Highlight the code cells now that we know what language they are
            for node in nodes:
                source = node.children[0]
                lexer = notebook.metadata.language_info.pygments_lexer
                source.attributes['language'] = lexer

            # Write certain cell outputs (e.g. images) to separate files, and
            # modify the metadata of the associated cells in 'notebook' to
            # include the path to the output file.
            write_notebook_output(notebook, output_dir, file_name)

            # Add doctree nodes for cell outputs.
            for node, cell in zip(nodes, notebook.cells):
                output_nodes = cell_output_to_nodes(
                    cell,
                    self.config.jupyter_execute_data_priority,
                    sphinx_abs_dir(self.env)
                )
                attach_outputs(output_nodes, node)


### Roles

def jupyter_download_role(name, rawtext, text, lineno, inliner):
    _, filetype = name.split(':')
    assert filetype in ('notebook', 'script')
    ext = '.ipynb' if filetype == 'notebook' else '.py'
    output_dir = sphinx_abs_dir(inliner.document.settings.env)
    download_file = text + ext
    node = download_reference(
        download_file, download_file,
        reftarget=os.path.join(output_dir, download_file)
    )
    return [node], []


### Utilities

def blank_nb(kernel_name):
    try:
        spec = get_kernel_spec(kernel_name)
    except NoSuchKernel as e:
        raise ExtensionError('Unable to find kernel', orig_exc=e)
    return nbformat.v4.new_notebook(metadata={
        'kernelspec': {
            'display_name': spec.display_name,
            'language': spec.language,
            'name': kernel_name,
        }
    })


def split_on(pred, it):
    """Split an iterator wherever a predicate is True."""

    counter = 0

    def count(x):
        nonlocal counter
        if pred(x):
            counter += 1
        return counter

    # Return iterable of lists to ensure that we don't lose our
    # place in the iterator
    return (list(x) for _, x in groupby(it, count))


def cell_output_to_nodes(cell, data_priority, dir):
    """Convert a jupyter cell with outputs and filenames to doctree nodes.

    Parameters
    ----------
    cell : jupyter cell
    data_priority : list of mime types
        Which media types to prioritize.
    dir : string
        Sphinx "absolute path" to the output folder, so it is a relative path
        to the source folder prefixed with ``/``.
    """
    to_add = []
    for index, output in enumerate(cell.get('outputs', [])):
        output_type = output['output_type']
        if (
            output_type == 'stream'
            and output['name'] == 'stdout'
        ):
            to_add.append(docutils.nodes.literal_block(
                text=output['text'],
                rawsource=output['text'],
            ))
        elif (
            output_type == 'error'
        ):
            traceback = '\n'.join(output['traceback'])
            text = nbconvert.filters.strip_ansi(traceback)
            to_add.append(docutils.nodes.literal_block(
                text=text,
                rawsource=text,
                language='ipythontb',
            ))
        elif (
            output_type in ('display_data', 'execute_result')
        ):
            try:
                # First mime_type by priority that occurs in output.
                mime_type = next(
                    x for x in data_priority if x in output['data']
                )
            except StopIteration:
                continue

            data = output['data'][mime_type]
            if mime_type.startswith('image'):
                # Sphinx treats absolute paths as being rooted at the source
                # directory, so make a relative path, which Sphinx treats
                # as being relative to the current working directory.
                filename = os.path.basename(
                    output.metadata['filenames'][mime_type]
                )
                uri = os.path.join(dir, filename)
                to_add.append(docutils.nodes.image(uri=uri))
            elif mime_type == 'text/html':
                to_add.append(docutils.nodes.raw(
                    text=data,
                    format='html'
                ))
            elif mime_type == 'text/latex':
                to_add.append(displaymath(
                    latex=data,
                    nowrap=False,
                    number=None,
                 ))
            elif mime_type == 'text/plain':
                to_add.append(docutils.nodes.literal_block(
                    text=data,
                    rawsource=data,
                ))

    return to_add


def attach_outputs(output_nodes, node):
    if node.attributes['hide_code']:
        node.children = []
    if not node.attributes['hide_output']:
        if node.attributes['code_below']:
            node.children = output_nodes + node.children
        else:
            node.children = node.children + output_nodes


def default_notebook_names(basename):
    """Return an interator yielding notebook names based off 'basename'"""
    yield basename
    for i in count(1):
        yield '_'.join((basename, str(i)))


def execute_cells(kernel_name, cells, execute_kwargs):
    """Execute Jupyter cells in the specified kernel and return the notebook."""
    notebook = blank_nb(kernel_name)
    notebook.cells = cells
    # Modifies 'notebook' in-place
    try:
        executenb(notebook, **execute_kwargs)
    except Exception as e:
        raise ExtensionError('Notebook execution failed', orig_exc=e)

    return notebook


def write_notebook_output(notebook, output_dir, notebook_name):
    """Extract output from notebook cells and write to files in output_dir.

    This also modifies 'notebook' in-place, adding metadata to each cell that
    maps output mime-types to the filenames the output was saved under.
    """
    resources = dict(
        unique_key=os.path.join(output_dir, notebook_name),
        outputs={}
    )

    # Modifies 'resources' in-place
    ExtractOutputPreprocessor().preprocess(notebook, resources)
    # Write the cell outputs to files where we can (images and PDFs),
    # as well as the notebook file.
    FilesWriter(build_directory=output_dir).write(
        nbformat.writes(notebook), resources,
        os.path.join(output_dir, notebook_name + '.ipynb')
    )
    # Write a script too.
    ext = notebook.metadata.language_info.file_extension
    contents = '\n\n'.join(cell.source for cell in notebook.cells)
    with open(os.path.join(output_dir, notebook_name + ext), 'w') as f:
        f.write(contents)


def output_directory(env):
    # Put output images inside the sphinx build directory to avoid
    # polluting the current working directory. We don't use a
    # temporary directory, as sphinx may cache the doctree with
    # references to the images that we write

    # Note: we are using an implicit fact that sphinx output directories are
    # direct subfolders of the build directory.
    return os.path.abspath(os.path.join(
        env.app.outdir, os.path.pardir, 'jupyter_execute'
    ))


def sphinx_abs_dir(env):
    # We write the output files into
    # output_directory / jupyter_execute / path relative to source directory
    # Sphinx expects download links relative to source file or relative to
    # source dir and prepended with '/'. We use the latter option.
    return '/' + os.path.relpath(
        os.path.abspath(os.path.join(
            output_directory(env),
            os.path.dirname(env.docname),
        )),
        os.path.abspath(env.app.srcdir)
    )


def setup(app):
    # Configuration
    app.add_config_value(
        'jupyter_execute_kwargs',
        dict(timeout=-1, allow_errors=True),
        'env'
    )
    app.add_config_value(
        'jupyter_execute_default_kernel',
        'python3',
        'env'
    )
    app.add_config_value(
        'jupyter_execute_data_priority',
        [
            'text/html',
            'image/svg+xml',
            'image/png',
            'image/jpeg',
            'text/latex',
            'text/plain'
        ],
        'env',
    )

    # JupyterKernelNode is just a doctree marker for the
    # ExecuteJupyterCells transform, so we don't actually render it.
    def skip(self, node):
        raise docutils.nodes.SkipNode

    app.add_node(
        JupyterKernelNode,
        html=(skip, None),
        latex=(skip, None),
        textinfo=(skip, None),
        text=(skip, None),
        man=(skip, None),
    )


    # JupyterCellNode is a container that holds the input and
    # any output, so we render it as a container.
    render_container = (
        lambda self, node: self.visit_container(node),
        lambda self, node: self.depart_container(node),
    )

    app.add_node(
        JupyterCellNode,
        html=render_container,
        latex=render_container,
        textinfo=render_container,
        text=render_container,
        man=render_container,
    )

    app.add_directive('jupyter-execute', JupyterCell)
    app.add_directive('jupyter-kernel', JupyterKernel)
    app.add_role('jupyter-download:notebook', jupyter_download_role)
    app.add_role('jupyter-download:script', jupyter_download_role)
    app.add_transform(ExecuteJupyterCells)

    # For syntax highlighting
    app.add_lexer('ipythontb', IPythonTracebackLexer())
    app.add_lexer('ipython', IPython3Lexer())

    return {'version': __version__}
