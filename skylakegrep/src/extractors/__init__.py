# SPDX-License-Identifier: Apache-2.0
"""Reference-edge extractors keyed by content-type.

Each module in this package exposes a single ``extract_edges(files, root)``
callable that returns a list of ``(src_path, dst_path)`` tuples — both
absolute or both expressed in the same coordinate space as the input files.

The ``reference_graph`` registry imports these extractors and dispatches per
file based on suffix / detected content type. New extractors plug in by
adding a module here and registering it in ``reference_graph.REFERENCE_EXTRACTORS``.
"""

from . import code, markdown

__all__ = ["code", "markdown"]
