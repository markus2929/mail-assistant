// The only script: "select all" checkboxes. Everything works without it.
document.addEventListener("change", function (ev) {
  var box = ev.target;
  if (!box.matches("[data-select-all]")) return;
  var scope = box.closest("main");
  scope.querySelectorAll('input[type=checkbox][name=ids]').forEach(function (c) { c.checked = box.checked; });
});
