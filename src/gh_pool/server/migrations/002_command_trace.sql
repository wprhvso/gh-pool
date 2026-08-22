-- The W3C trace context the client was in when it enqueued the command.
-- Nullable and without a default, so this is a catalogue change on an existing
-- table rather than a rewrite of every row already in it.
alter table commands add column traceparent text;
alter table commands add column tracestate text;
