# Vendored front-end libraries

These are checked in rather than fetched from a CDN because rigsmith is a
tool for talking to a device on your own network, and has to work with no
internet at all. Nothing in the UI reaches outside the local server.

| File | Library | Version | Licence |
|---|---|---|---|
| `sortable.min.js` | [SortableJS](https://github.com/SortableJS/Sortable) | 1.15.6 | MIT |
| `uplot.iife.min.js`, `uplot.min.css` | [uPlot](https://github.com/leeoniya/uPlot) | 1.6.31 | MIT |
| `wavesurfer.min.js`, `wavesurfer.regions.min.js` | [wavesurfer.js](https://github.com/katspaugh/wavesurfer.js) | 7.8.6 | BSD-3-Clause |

`wavesurfer.regions.min.js` extends the `WaveSurfer` global rather than
defining its own, so it has to load after `wavesurfer.min.js`.

To update one, replace the file and change the version above. There is no
build step and no `node_modules` - that is deliberate, so `pip install
rigsmith` is the only thing anyone needs to run the UI or modify it.
