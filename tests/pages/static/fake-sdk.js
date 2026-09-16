// Stand-in for a real MarTech SDK. Deliberately does the awkward things a
// real one does: creates a global, writes storage, beacons out, and mentions
// a hostname it never actually calls on this page type.
(function () {
  window.FakeVendor = {
    version: '2.4.1',
    partnerId: 'fv-korea-001',
    track: function () {},
    endpoints: {
      collect: 'https://collect.fakevendor-cdn.com/v2/event',
      recommend: 'https://reco.fakevendor-cdn.com/items'
    }
  };
  document.cookie = 'fv_uid=abc123; path=/';
  try {
    localStorage.setItem('fv.session', '1');
    sessionStorage.setItem('fv_temp', '1');
  } catch (e) {}
  var el = document.createElement('fv-widget');
  el.setAttribute('data-fv-campaign', 'welcome');
  document.body.appendChild(el);
  fetch('/collect/event?uid=abc123').catch(function () {});
  console.log('[FakeVendor] initialised partner fv-korea-001');
})();
