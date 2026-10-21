// Standalone entry: the module imports the shared fragments need (Mana injects the same names),
// then the fragments, the HTTP host and the mount. build.mjs splices the fragments in at the marker.
import React from 'react';
import { createRoot } from 'react-dom/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
// The rig config editor's YAML reader (host.loadYaml); the Mana host loads the same package on demand.
import * as YAML from 'yaml';

/* @@PLATFORM_SOURCES@@ */

createRoot(document.getElementById('root')).render(<StandaloneRoot />);
