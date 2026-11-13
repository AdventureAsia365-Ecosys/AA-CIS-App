"""AA-1093 part 3 — per-tenant backup of the CIS tables and its restore drill (gap M27: CIS had only whole-instance
PITR). ``catalogue`` decides what a backup holds, ``export`` writes it, ``restore_drill`` proves it restores."""
