-- The model is chosen per role (home_core.llm.AiSettings); BABA's one role is
-- `image`, so the model a provider had becomes that role's.
ALTER TABLE ai_settings ADD COLUMN models jsonb NOT NULL DEFAULT '{}'::jsonb;
UPDATE ai_settings SET models = jsonb_build_object('image', model);
ALTER TABLE ai_settings DROP COLUMN model;
