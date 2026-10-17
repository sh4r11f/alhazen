// Standalone entry: the module imports the shared fragments need (Mana injects the same names),
// then the fragments, the HTTP host and the mount. build.mjs splices the fragments in at the marker.
import React from 'react';
import { createRoot } from 'react-dom/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/* @@PLATFORM_SOURCES@@ */

createRoot(document.getElementById('root')).render(<StandaloneRoot />);
