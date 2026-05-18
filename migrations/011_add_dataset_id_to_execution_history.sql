ALTER TABLE execution_history ADD COLUMN dataset_id INTEGER REFERENCES datasets(id);
