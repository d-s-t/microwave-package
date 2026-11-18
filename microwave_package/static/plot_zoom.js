// Keeps a figure's zoom across the re-renders of its iframe (a filter
// ticked, a theme flip, a run renamed). The figure is rebuilt by the
// server each time, so the ranges are read back out of it, remembered
// here, and put back once the new one has loaded.
//
//   var zoom = PlotZoom(frame);
//   zoom.setKey("freq|s11");   // a different X/Y choice forgets the zoom
//   zoom.reset();              // forget it on request
//
// A reset inside the figure (double-click, the home button) turns the
// axis back to autorange, which is read the same way and so is kept too.
(function () {
  window.PlotZoom = function (frame) {
    var saved = null, key = null;

    function plotOf() {
      try { return frame.contentDocument && frame.contentDocument.querySelector(".js-plotly-plot"); }
      catch (e) { return null; }
    }

    function read() {
      var plot = plotOf(), layout = plot && plot._fullLayout;
      if (!layout || !layout.xaxis || !layout.yaxis) { return; }
      saved = {
        x: layout.xaxis.autorange ? null : layout.xaxis.range.slice(),
        y: layout.yaxis.autorange ? null : layout.yaxis.range.slice(),
      };
    }

    frame.addEventListener("load", function () {
      var plot = plotOf(), win = frame.contentWindow;
      if (!plot || !win || !win.Plotly || !plot.on) { return; }
      var update = {};
      if (saved && saved.x) { update["xaxis.range"] = saved.x; }
      if (saved && saved.y) { update["yaxis.range"] = saved.y; }
      if (Object.keys(update).length) { win.Plotly.relayout(plot, update); }
      plot.on("plotly_relayout", read);
    });

    return {
      setKey: function (next) { if (next !== key) { key = next; saved = null; } },
      reset: function () { saved = null; },
      current: function () { return saved; },
    };
  };
})();
