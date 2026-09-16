// A tag-manager style loader: the vendor script is two hops from the HTML,
// which is exactly the shape a <head>-only scanner misses.
//
// Note the insertion point. This runs from <head> while the parser is still
// working, so `document.body` does not exist yet — the classic GTM snippet
// inserts before the first <script> element for precisely this reason.
(function () {
  window.fakeDataLayer = window.fakeDataLayer || [];
  var s = document.createElement('script');
  s.async = true;
  s.src = '/static/fake-sdk.js';
  var first = document.getElementsByTagName('script')[0];
  first.parentNode.insertBefore(s, first);
})();
