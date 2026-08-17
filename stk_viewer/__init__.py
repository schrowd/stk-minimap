"""
stk_viewer - render a SuperTuxKart track's minimap to a PNG, and play
replays back on top of it.

STK does not ship minimap images. It builds them at track load time by taking
the track's *driveline graph* (race tracks) or *navmesh* (arenas / soccer
fields), pushing the quads into a mesh, and rendering that mesh with a
top-down orthographic camera into a render target texture.  See
`Graph::makeMiniMap` / `Graph::createMesh` in src/tracks/graph.cpp of stk-code.

See docs/NOTES.md for the research behind the reimplementation, and
docs/SYNCNOTES.md for the SuperTuxKart-sync half in stk_viewer.sync/.
"""

__version__ = "1.6.1"
