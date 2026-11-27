frappe.ui.form.on("Epicollect Project", {
	refresh(frm) {
		if (frm.is_new()) return;
		const project = frm.doc.name;
		const queue = (method, args, freeze_message) => {
			// Save first, so the job runs with the settings shown on screen.
			const saved = frm.is_dirty() ? frm.save() : Promise.resolve();
			saved
				.then(() => frappe.call({ method, args: { project, ...args }, freeze: true, freeze_message }))
				.then((r) => {
					const res = r.message || {};
					frappe.show_alert({ message: __(res.message || ""), indicator: res.queued ? "green" : "orange" }, 7);
				});
		};
		const sync = __("Sync");
		frm.add_custom_button(__("Sync Now"), () => queue("epicollect_sync.sync.sync_now", {}, __("Queuing sync...")), sync);
		frm.add_custom_button(__("Check for Deleted Entries Now"), () => {
			frappe.confirm(__("Re-read every entry of this project from Epicollect and flag the ones deleted there. This downloads the whole project. Continue?"), () =>
				queue("epicollect_sync.sync.sync_now", { full_check: 1 }, __("Queuing full check...")));
		}, sync);
		frm.add_custom_button(__("Reset Checkpoints"), () => {
			frappe.confirm(__("The next sync will re-fetch every entry of this project. Continue?"), () => {
				frappe.call({ method: "epicollect_sync.sync.reset_checkpoints", args: { project } })
					.then(() => frappe.show_alert({ message: __("Checkpoints cleared."), indicator: "green" }));
			});
		}, sync);
		const setup = __("Set Up");
		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({ method: "epicollect_sync.sync.test_connection", args: { project }, freeze: true, freeze_message: __("Connecting...") })
				.then((r) => {
					const m = r.message;
					frappe.msgprint({
						title: __("Connected"), indicator: "green",
						message: __("Project {0}: {1} form(s) ({2}) and {3} branch question(s).",
							[frappe.utils.escape_html(m.name), m.forms.length, m.forms.map(frappe.utils.escape_html).join(", "), m.branches]),
					});
					frm.reload_doc();
				});
		}, setup);
		frm.add_custom_button(__("Set Up Forms and Fields"), () => {
			frappe.confirm(__("Create a DocType for every form and branch of this project, one field per question with number, date and location columns, and a dashboard. Columns are filled for entries already synced. This can take several minutes. Continue?"), () =>
				queue("epicollect_sync.sync.set_up", {}, __("Queuing set-up...")));
		}, setup);
		frm.add_custom_button(__("Open Dashboard"), () => frappe.set_route("dashboard-view", `Epicollect ${project}`));
	},
});
