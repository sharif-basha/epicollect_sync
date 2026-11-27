import re

import frappe
from frappe.model.document import Document
from frappe.utils import validate_email_address


class EpicollectProject(Document):
	def validate(self):
		self.project_slug = (self.project_slug or "").strip().strip("/").split("/")[-1]
		if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.project_slug or ""):
			frappe.throw("Project Slug should be the lower-case part after /project/ in the Epicollect address, "
						 "for example household-survey.")
		if self.per_page and not 1 <= self.per_page <= 1000:
			frappe.throw("Entries per Request must be between 1 and 1000.")
		bad = [e for e in re.split(r"[\s,;]+", self.notify_emails or "") if e and not validate_email_address(e)]
		if bad:
			frappe.throw(f"Not a valid email address: {', '.join(bad)}")
		if self.inactivity_days and self.inactivity_days < 0:
			self.inactivity_days = 0
