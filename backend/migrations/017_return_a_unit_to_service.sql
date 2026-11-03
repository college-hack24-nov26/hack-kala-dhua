-- A vehicle could be taken out of the fleet and never put back.
--
-- `field_status_kinds` has `reopened` ("Back in service") for a **lifeline** —
-- a hospital that reopens, a shelter that empties — and no equivalent for a
-- **resource**. So a crew reporting a puncture set the truck to `offline` with
-- `unavailable_reason = 'Puncture'`, its task was released, and from that
-- moment there was no action, on any screen, for any role, that returned it to
-- the pool. The vehicle was gone for the rest of the event.
--
-- That is why the console can count "units out of the fleet" and an officer can
-- do nothing about the number: the verb did not exist.
--
-- `makes_offline` is false and the severity is `info`, so it travels the
-- ordinary path rather than the one that takes a unit out — the handler in
-- `personas.py` gives it its own branch, because "noted" is not what this
-- means.

insert into field_status_kinds (id, label, makes_offline, severity, applies_to)
values ('back_in_service', 'Back in service', false, 'info', 'resource')
on conflict (id) do nothing;

-- The other half of the same bug. `task_complete` already returned a unit to
-- `available`, and left `unavailable_reason` exactly as it was — so a truck
-- that had a puncture, was repaired, and closed a task afterwards sat in the
-- fleet as available while every screen that reads that column still said
-- "Puncture". A reason for an unavailability that has ended is not stale data,
-- it is wrong data, and it is read by people deciding who to send.
update resources
   set unavailable_reason = null
 where status = 'available'
   and unavailable_reason is not null;
