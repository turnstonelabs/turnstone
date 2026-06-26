/* project_creator.js — the inline "create a project" widget.
 *
 * A name input + Save + Cancel that appears in place (no native popup), used by
 * every creation picker that offers "+ New project…": the console launcher
 * composer, the standalone new-workstream dialog, and the standalone dashboard
 * quick-create.  Save POSTs the project, refreshes the shared cache (so the new
 * project lands in every picker + the rail), then hands the created row back to
 * the caller to select; Cancel / Escape closes without creating.
 *
 * House style: programmatic DOM (no innerHTML), ES module + a
 * `window.TurnstoneProjectCreator` bridge for the classic app.js bundles.
 */

import { createProject, refreshProjects } from "./projects.js";

/**
 * @param {{onCreated?: (project) => void, onClose?: () => void}} opts
 * @returns {{el: HTMLElement, open: () => void, close: () => void, isOpen: () => boolean}}
 */
export function makeProjectCreator(opts) {
  opts = opts || {};
  const onCreated =
    typeof opts.onCreated === "function" ? opts.onCreated : function () {};
  const onClose =
    typeof opts.onClose === "function" ? opts.onClose : function () {};

  const root = document.createElement("div");
  root.className = "project-creator";
  root.hidden = true;

  const input = document.createElement("input");
  input.type = "text";
  input.className = "project-creator-input";
  input.placeholder = "New project name";
  input.setAttribute("aria-label", "New project name");
  input.maxLength = 200;
  input.autocomplete = "off";

  const save = document.createElement("button");
  save.type = "button";
  save.className = "project-creator-save";
  save.textContent = "Save";

  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "project-creator-cancel";
  cancel.textContent = "Cancel";
  cancel.setAttribute("aria-label", "Cancel new project");

  const err = document.createElement("div");
  err.className = "project-creator-error";
  err.setAttribute("role", "alert");
  err.hidden = true;

  root.append(input, save, cancel, err);

  let busy = false;

  function close() {
    root.hidden = true;
    input.value = "";
    err.hidden = true;
    busy = false;
    save.disabled = false;
    onClose();
  }

  function open() {
    err.hidden = true;
    input.value = "";
    busy = false;
    save.disabled = false;
    root.hidden = false;
    input.focus();
  }

  function showError(msg) {
    err.textContent = msg;
    err.hidden = false;
  }

  function submit() {
    if (busy) return;
    const name = (input.value || "").trim();
    if (!name) {
      showError("Name is required");
      input.focus();
      return;
    }
    busy = true;
    save.disabled = true;
    err.hidden = true;
    createProject(name).then(function (res) {
      if (!res.ok || !res.data || !res.data.project_id) {
        busy = false;
        save.disabled = false;
        showError((res.data && res.data.error) || "Failed to create project");
        return;
      }
      const created = res.data;
      // Refresh the shared cache first so the caller's repopulate sees the new
      // project, then close + hand it back to select.
      refreshProjects().then(function () {
        close();
        onCreated(created);
      });
    });
  }

  save.addEventListener("click", submit);
  cancel.addEventListener("click", close);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  });

  return {
    el: root,
    open: open,
    close: close,
    isOpen: function () {
      return !root.hidden;
    },
  };
}

window.TurnstoneProjectCreator = { make: makeProjectCreator };
