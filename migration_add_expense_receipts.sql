ALTER TABLE expenses
ADD COLUMN IF NOT EXISTS receipt_path TEXT;

ALTER TABLE expenses
ADD COLUMN IF NOT EXISTS receipt_original_name TEXT;

ALTER TABLE expenses
ADD COLUMN IF NOT EXISTS receipt_media_type TEXT;
