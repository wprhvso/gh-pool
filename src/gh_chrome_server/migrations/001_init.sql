create type session_status as enum ('pending', 'active', 'closed', 'dead');
create type command_status as enum ('queued', 'started', 'finished', 'failed');

create table profiles (
    name       text primary key,
    size       bigint,
    stale      boolean not null default false,
    updated_at timestamptz
);

create table sessions (
    id           uuid primary key,
    status       session_status not null default 'pending',
    params       jsonb not null,
    profile      text references profiles(name) on delete set null,
    persist      boolean not null default true,
    state_stale  boolean not null default false,
    last_seq     bigint not null default 0,
    last_cmd_seq bigint not null default 0,
    heartbeat_at timestamptz,
    created_at   timestamptz not null default now(),
    ready_at     timestamptz,
    closed_at    timestamptz
);

create index sessions_active_idx on sessions (status) where status in ('pending', 'active');

create table commands (
    id          uuid primary key,
    session_id  uuid not null references sessions(id) on delete cascade,
    seq         bigint not null,
    method      text not null,
    args        jsonb not null,
    timeout_ms  integer not null,
    status      command_status not null default 'queued',
    result      jsonb,
    error       jsonb,
    queued_at   timestamptz not null default now(),
    started_at  timestamptz,
    finished_at timestamptz,
    unique (session_id, seq)
);

create index commands_queue_idx on commands (session_id, seq) where status = 'queued';
create index commands_started_idx on commands (started_at) where status = 'started';

create table events (
    session_id uuid not null references sessions(id) on delete cascade,
    seq        bigint not null,
    type       text not null,
    data       jsonb not null,
    created_at timestamptz not null default now(),
    primary key (session_id, seq)
);

create table files (
    id         uuid primary key,
    session_id uuid not null references sessions(id) on delete cascade,
    name       text not null,
    size       bigint not null,
    created_at timestamptz not null default now()
);

create table downloads (
    session_id uuid not null references sessions(id) on delete cascade,
    name       text not null,
    size       bigint not null,
    created_at timestamptz not null default now(),
    primary key (session_id, name)
);
