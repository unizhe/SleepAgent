-- sleepagent:transactional=true
-- Durable command reservation, operation fencing, invocation reconciliation,
-- destination delivery and consumer deduplication.

ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN scope_protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN subject_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_contract CHECK (
    scope_protocol_version < 2 OR (
      namespace_generation >= 1
      AND subject_id IS NOT NULL
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN subject_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN authorization_snapshot_json JSONB;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 8;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN fencing_token TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN worker_instance TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN heartbeat_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_contract CHECK (
    protocol_version < 2 OR (
      namespace_generation >= 1
      AND subject_id IS NOT NULL
      AND authorization_snapshot_json IS NOT NULL
      AND jsonb_typeof(authorization_snapshot_json) = 'object'
      AND authorization_snapshot_json ?& ARRAY[
        'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
      ]
      AND max_attempts >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND (
        status <> 'running'
        OR (lease_generation >= 1 AND fencing_token IS NOT NULL
          AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_subject_fk
  FOREIGN KEY (namespace_id, data_mode, subject_id)
  REFERENCES backend_subjects (
    namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_subject_unique
  UNIQUE (raw_ingress_record_id, namespace_id, data_mode, subject_id);
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_subject_fk
  FOREIGN KEY (namespace_id, data_mode, subject_id)
  REFERENCES backend_subjects (
    namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_raw_subject_fk
  FOREIGN KEY (
    raw_ingress_record_id, namespace_id, data_mode, subject_id
  ) REFERENCES sleep_domain_raw_inbox (
    raw_ingress_record_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_operations
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_operations
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN id_scheme TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_operations
  ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'user';
ALTER TABLE sleep_domain_operations
  ADD COLUMN semantic_key TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN queue_name TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sleep_domain_operations
  ADD COLUMN available_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_operations
  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5;
ALTER TABLE sleep_domain_operations
  ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sleep_domain_operations
  ADD COLUMN fencing_token TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN worker_instance TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN heartbeat_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_operations
  ADD COLUMN authorization_snapshot_json JSONB;
ALTER TABLE sleep_domain_operations
  ADD COLUMN workload_authorization_snapshot_json JSONB;
ALTER TABLE sleep_domain_operations
  ADD COLUMN policy_sha256 TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN outcome_class TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN dead_lettered_at TIMESTAMPTZ;

ALTER TABLE sleep_domain_operations ALTER COLUMN actor_id DROP NOT NULL;

ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_subject_scope_unique
  UNIQUE (operation_id, namespace_id, data_mode, subject_id);

UPDATE sleep_domain_operations
SET available_at = COALESCE(available_at, created_at),
    queue_name = COALESCE(queue_name, operation_type)
WHERE available_at IS NULL OR queue_name IS NULL;

ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_protocol_v2_check CHECK (
    protocol_version < 2 OR (
      id_scheme = 'uuidv7'
      AND operation_id ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      AND namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND origin_kind IN ('user', 'system')
      AND semantic_key IS NOT NULL
      AND queue_name IS NOT NULL
      AND available_at IS NOT NULL
      AND max_attempts >= 1
      AND policy_sha256 ~ '^[0-9a-f]{64}$'
      AND (
        (origin_kind = 'user' AND actor_id IS NOT NULL
          AND authorization_snapshot_json IS NOT NULL
          AND workload_authorization_snapshot_json IS NULL
          AND jsonb_typeof(authorization_snapshot_json) = 'object'
          AND authorization_snapshot_json ?& ARRAY[
            'authorization_epoch', 'privacy_epoch',
            'retrieval_policy_epoch'
          ])
        OR
        (origin_kind = 'system' AND actor_id IS NULL
          AND authorization_snapshot_json IS NULL
          AND workload_authorization_snapshot_json IS NOT NULL
          AND jsonb_typeof(workload_authorization_snapshot_json) = 'object'
          AND workload_authorization_snapshot_json ?& ARRAY[
            'authorization_epoch', 'privacy_epoch',
            'retrieval_policy_epoch'
          ])
      )
      AND (
        status <> 'running'
        OR (lease_generation >= 1 AND fencing_token IS NOT NULL
          AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sleep_domain_operation_semantic_v2
  ON sleep_domain_operations (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''),
    operation_type, semantic_key
  )
  WHERE protocol_version >= 2;

CREATE INDEX IF NOT EXISTS idx_sleep_domain_operation_claim_v2
  ON sleep_domain_operations (
    data_mode, queue_name, status, priority DESC, available_at, created_at
  )
  WHERE protocol_version >= 2;

CREATE TABLE IF NOT EXISTS backend_command_receipts (
  command_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  service_principal_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  route_template TEXT NOT NULL,
  caller_idempotency_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  operation_id TEXT NOT NULL,
  authorization_snapshot_json JSONB NOT NULL,
  receipt_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ,
  UNIQUE (
    service_principal_id, actor_id, route_template, caller_idempotency_key
  ),
  UNIQUE (namespace_id, data_mode, command_receipt_id),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (expires_at IS NULL OR expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_backend_command_receipt_operation
  ON backend_command_receipts (namespace_id, data_mode, operation_id);

CREATE TABLE IF NOT EXISTS backend_invocations (
  invocation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  invocation_kind TEXT NOT NULL CHECK (
    invocation_kind IN ('model', 'provider', 'external_sink')
  ),
  invocation_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  provider_request_id TEXT,
  current_state TEXT NOT NULL CHECK (
    current_state IN (
      'reserved', 'send_started', 'outcome_possible', 'response_received',
      'known_failed',
      'outcome_unknown', 'reconciled'
    )
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  dispatch_permit_at TIMESTAMPTZ,
  response_sha256 TEXT CHECK (
    response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'
  ),
  reserved_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (namespace_id, data_mode, operation_id, invocation_key),
  UNIQUE (invocation_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_invocation_journal (
  invocation_event_id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  from_state TEXT,
  to_state TEXT NOT NULL CHECK (
    to_state IN (
      'reserved', 'send_started', 'outcome_possible', 'response_received',
      'known_failed',
      'outcome_unknown', 'reconciled'
    )
  ),
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (invocation_id, sequence),
  FOREIGN KEY (invocation_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_invocations (
      invocation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_operation_heartbeats (
  heartbeat_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  worker_instance TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, lease_generation, recorded_at),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_operation_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  checkpoint_sequence BIGINT NOT NULL CHECK (checkpoint_sequence >= 1),
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  checkpoint_kind TEXT NOT NULL,
  checkpoint_sha256 TEXT NOT NULL CHECK (
    checkpoint_sha256 ~ '^[0-9a-f]{64}$'
  ),
  checkpoint_json JSONB NOT NULL,
  visible_to_queries BOOLEAN NOT NULL DEFAULT FALSE CHECK (
    visible_to_queries = FALSE
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, checkpoint_sequence),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_protocol_v2_scope CHECK (
    protocol_version < 2 OR (
      namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  ) NOT VALID;
ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_subject_scope_unique
  UNIQUE (event_id, namespace_id, data_mode, subject_id);

CREATE TABLE IF NOT EXISTS backend_delivery_intents (
  delivery_intent_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  handler_name TEXT NOT NULL,
  semantic_effect_key TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  aggregate_sequence BIGINT NOT NULL CHECK (aggregate_sequence >= 1),
  predecessor_sequence BIGINT CHECK (predecessor_sequence >= 1),
  status TEXT NOT NULL CHECK (
    status IN (
      'pending', 'running', 'dispatching', 'delivered', 'retry',
      'dead_letter', 'outcome_unknown', 'cancelled'
    )
  ),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts >= 1),
  priority INTEGER NOT NULL DEFAULT 0,
  available_at TIMESTAMPTZ NOT NULL,
  lease_generation BIGINT NOT NULL DEFAULT 0,
  fencing_token TEXT,
  worker_instance TEXT,
  lease_expires_at TIMESTAMPTZ,
  dispatch_permit_at TIMESTAMPTZ,
  authorization_snapshot_json JSONB NOT NULL CHECK (
    jsonb_typeof(authorization_snapshot_json) = 'object'
    AND authorization_snapshot_json ?& ARRAY[
      'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
    ]
  ),
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  intent_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  delivered_at TIMESTAMPTZ,
  UNIQUE (destination, semantic_effect_key),
  UNIQUE (namespace_id, data_mode, delivery_intent_id),
  UNIQUE (delivery_intent_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (source_event_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_domain_outbox (
      event_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    predecessor_sequence IS NULL
    OR predecessor_sequence < aggregate_sequence
  ),
  CHECK (
    status NOT IN ('running', 'dispatching')
    OR (lease_generation >= 1 AND fencing_token IS NOT NULL
      AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_delivery_claim
  ON backend_delivery_intents (
    data_mode, destination, status, priority DESC, available_at, created_at
  );

CREATE TABLE IF NOT EXISTS backend_delivery_journal (
  delivery_event_id TEXT PRIMARY KEY,
  delivery_intent_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  from_state TEXT,
  to_state TEXT NOT NULL,
  lease_generation BIGINT,
  fencing_token TEXT,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (delivery_intent_id, sequence),
  FOREIGN KEY (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_delivery_intents (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_consumer_inbox (
  consumer_id TEXT NOT NULL,
  delivery_intent_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  semantic_effect_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  received_at TIMESTAMPTZ NOT NULL,
  applied_at TIMESTAMPTZ,
  result_sha256 TEXT CHECK (
    result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'
  ),
  PRIMARY KEY (consumer_id, delivery_intent_id),
  UNIQUE (consumer_id, semantic_effect_key),
  FOREIGN KEY (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_delivery_intents (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_consumer_checkpoints (
  consumer_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  last_applied_sequence BIGINT NOT NULL CHECK (last_applied_sequence >= 0),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (
    consumer_id, namespace_id, data_mode, aggregate_type, aggregate_id
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

-- Reuse the established role-view authority, but make v2 projections carry
-- their exact replay scope, source, policy and revocation epochs.
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN source_fact_snapshot_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN source_state_version BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN authorization_epoch BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN privacy_epoch BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN retrieval_policy_epoch BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN policy_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN projection_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD CONSTRAINT sleep_domain_role_view_v2_contract CHECK (
    protocol_version < 2 OR (
      namespace_generation >= 1
      AND source_fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
      AND source_state_version >= 1
      AND authorization_epoch >= 1
      AND privacy_epoch >= 1
      AND retrieval_policy_epoch >= 1
      AND policy_sha256 ~ '^[0-9a-f]{64}$'
      AND projection_sha256 ~ '^[0-9a-f]{64}$'
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  ) NOT VALID;

CREATE TABLE IF NOT EXISTS backend_product_attempts (
  product_attempt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  attempt_sequence BIGINT NOT NULL CHECK (attempt_sequence >= 1),
  attempt_state TEXT NOT NULL CHECK (
    attempt_state IN (
      'prepared', 'committed', 'abandoned', 'outcome_unknown'
    )
  ),
  query_visible BOOLEAN NOT NULL DEFAULT FALSE,
  fact_snapshot_sha256 TEXT NOT NULL CHECK (
    fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  state_version BIGINT NOT NULL CHECK (state_version >= 1),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL CHECK (
    retrieval_policy_epoch >= 1
  ),
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  attempt_sha256 TEXT NOT NULL CHECK (attempt_sha256 ~ '^[0-9a-f]{64}$'),
  attempt_json JSONB NOT NULL,
  prepared_at TIMESTAMPTZ NOT NULL,
  committed_at TIMESTAMPTZ,
  UNIQUE (operation_id, attempt_sequence),
  CHECK (
    (attempt_state = 'committed' AND query_visible = TRUE
      AND committed_at IS NOT NULL)
    OR (attempt_state <> 'committed' AND query_visible = FALSE
      AND committed_at IS NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_pending_handles (
  handle_id TEXT PRIMARY KEY,
  handle_kind TEXT NOT NULL CHECK (
    handle_kind IN ('confirmation', 'answer')
  ),
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  target_resource_type TEXT NOT NULL,
  target_resource_id TEXT NOT NULL,
  target_state_version BIGINT NOT NULL CHECK (target_state_version >= 1),
  target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
  fact_snapshot_sha256 TEXT NOT NULL CHECK (
    fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  care_profile_state_version BIGINT NOT NULL CHECK (
    care_profile_state_version >= 1
  ),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL CHECK (
    retrieval_policy_epoch >= 1
  ),
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK (
    status IN ('pending', 'consumed', 'expired', 'revoked')
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  consumed_by_command_receipt_id TEXT,
  handle_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (
    namespace_id, data_mode, namespace_generation, target_resource_type,
    target_resource_id, role, handle_kind, target_state_version
  ),
  CHECK (
    (status = 'pending' AND consumed_at IS NULL
      AND consumed_by_command_receipt_id IS NULL)
    OR (status = 'consumed' AND consumed_at IS NOT NULL
      AND consumed_by_command_receipt_id IS NOT NULL)
    OR (status IN ('expired', 'revoked') AND consumed_at IS NULL
      AND consumed_by_command_receipt_id IS NULL)
  ),
  CHECK (expires_at > created_at),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_pending_handle_expiry
  ON backend_pending_handles (
    namespace_id, data_mode, status, expires_at, handle_id
  );

-- ScenarioClock is replay observation time only.  ControlClock remains
-- PostgreSQL server time and is never mutable through this table.
CREATE TABLE IF NOT EXISTS backend_replay_scenario_clocks (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  scenario_now TIMESTAMPTZ NOT NULL,
  clock_version BIGINT NOT NULL DEFAULT 1 CHECK (clock_version >= 1),
  last_command_operation_id TEXT NOT NULL,
  command_lease_generation BIGINT NOT NULL CHECK (
    command_lease_generation >= 1
  ),
  command_fencing_token TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (
    namespace_id, data_mode, namespace_generation, run_id, arm_id
  ),
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
    REFERENCES backend_replay_arms (
      namespace_id, namespace_generation, run_id, arm_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (last_command_operation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_demo_seed_allowlist (
  seed_id TEXT PRIMARY KEY,
  seed_sha256 TEXT NOT NULL UNIQUE CHECK (seed_sha256 ~ '^[0-9a-f]{64}$'),
  schema_version TEXT NOT NULL,
  generator_version TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (NOT (metadata_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE TABLE IF NOT EXISTS backend_demo_traces (
  demo_trace_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  seed_id TEXT NOT NULL
    REFERENCES backend_demo_seed_allowlist(seed_id) ON DELETE RESTRICT,
  command_operation_id TEXT NOT NULL,
  scenario_clock_version BIGINT NOT NULL CHECK (scenario_clock_version >= 1),
  trace_state TEXT NOT NULL CHECK (
    trace_state IN ('requested', 'running', 'committed', 'failed')
  ),
  trace_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (command_operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (NOT (trace_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE OR REPLACE FUNCTION sleepagent_reject_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS backend_invocation_journal_immutable
  ON backend_invocation_journal;
CREATE TRIGGER backend_invocation_journal_immutable
BEFORE UPDATE OR DELETE ON backend_invocation_journal
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_delivery_journal_immutable
  ON backend_delivery_journal;
CREATE TRIGGER backend_delivery_journal_immutable
BEFORE UPDATE OR DELETE ON backend_delivery_journal
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_operation_checkpoint_immutable
  ON backend_operation_checkpoints;
CREATE TRIGGER backend_operation_checkpoint_immutable
BEFORE UPDATE OR DELETE ON backend_operation_checkpoints
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_operation_heartbeat_immutable
  ON backend_operation_heartbeats;
CREATE TRIGGER backend_operation_heartbeat_immutable
BEFORE UPDATE OR DELETE ON backend_operation_heartbeats
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE OR REPLACE FUNCTION sleepagent_protect_committed_event_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.protocol_version >= 2 THEN
    RAISE EXCEPTION 'v2 committed domain events are append-only';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sleep_domain_committed_event_v2_immutable
  ON sleep_domain_domain_outbox;
CREATE TRIGGER sleep_domain_committed_event_v2_immutable
BEFORE UPDATE OR DELETE ON sleep_domain_domain_outbox
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_committed_event_v2();

CREATE OR REPLACE FUNCTION sleepagent_claim_normalization_work(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  work_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  raw_ingress_record_id TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
BEGIN
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted normalization claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT work.work_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_normalization_work AS work
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = work.namespace_id
     AND namespace_row.data_mode = work.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = work.namespace_id
     AND epoch_row.data_mode = work.data_mode
     AND epoch_row.subject_id = work.subject_id
    WHERE work.protocol_version >= 2
      AND work.data_mode = deployment_mode
      AND (
        work.status IN ('pending', 'retry')
        OR (work.status = 'running'
          AND work.lease_expires_at <= clock_timestamp())
      )
      AND work.available_at <= clock_timestamp()
      AND work.attempt_count < work.max_attempts
      AND namespace_row.status = 'active'
      AND work.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND work.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND work.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = work.namespace_id
          AND grant_row.data_mode = work.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'normalization'
      )
    ORDER BY work.available_at, work.created_at, work.work_id
    FOR UPDATE OF namespace_row, work SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_normalization_work AS work
    SET status = 'running',
        attempt_count = work.attempt_count + 1,
        lease_generation = work.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE work.work_id = candidate.work_id
    RETURNING work.work_id, work.namespace_id, work.data_mode,
      work.namespace_generation, work.run_id, work.arm_id,
      work.subject_id, work.raw_ingress_record_id,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      work.lease_generation, work.fencing_token
  )
  SELECT claimed.work_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.raw_ingress_record_id,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_normalization_work(TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_normalization_work(
  target_work_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid normalization heartbeat duration';
  END IF;
  UPDATE public.sleep_domain_normalization_work
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE work_id = target_work_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_normalization_work(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_normalization_work(
  target_work_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  requested_next_available_at TIMESTAMPTZ,
  requested_error_code TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'succeeded', 'retry', 'dead_letter', 'quarantined'
  ) OR (requested_final_status = 'retry'
    AND requested_next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid normalization final status';
  END IF;
  UPDATE public.sleep_domain_normalization_work
  SET status = requested_final_status,
      available_at = COALESCE(requested_next_available_at, available_at),
      last_error_code = requested_error_code,
      lease_expires_at = NULL,
      lease_owner = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE work_id = target_work_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_normalization_work(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ, TEXT
) FROM PUBLIC;

-- Cross-namespace claim is the only SECURITY DEFINER work scan.  It derives
-- principal and deployment data mode from trusted connection context, checks
-- the principal grant/handler allowlist, uses PostgreSQL server time, and
-- returns only the ID plus the exact fence needed by a subsequent scoped UoW.
CREATE OR REPLACE FUNCTION sleepagent_claim_operation(
  requested_queue TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  operation_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  operation_type TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
  caller_process_role TEXT := NULLIF(
    current_setting('sleepagent.process_role', TRUE), ''
  );
BEGIN
  IF principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL OR caller_process_role <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION
      'trusted worker principal/data mode/purpose context is required';
  END IF;
  IF requested_queue IS NULL OR requested_queue = ''
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid operation claim parameters';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT op.operation_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_operations AS op
    JOIN public.backend_namespaces AS ns
      ON ns.namespace_id = op.namespace_id
     AND ns.data_mode = op.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = op.namespace_id
     AND epoch_row.data_mode = op.data_mode
     AND epoch_row.subject_id = op.subject_id
    WHERE op.protocol_version >= 2
      AND op.data_mode = deployment_mode
      AND op.queue_name = requested_queue
      AND (
        op.status IN ('pending', 'retry')
        OR (op.status = 'running'
          AND op.lease_expires_at <= clock_timestamp())
      )
      AND op.available_at <= clock_timestamp()
      AND op.attempt_count < op.max_attempts
      AND ns.status = 'active'
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'authorization_epoch' = epoch_row.authorization_epoch::text
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'privacy_epoch' = epoch_row.privacy_epoch::text
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND (
        SELECT COUNT(*)
        FROM public.sleep_domain_operations AS active
        WHERE active.namespace_id = op.namespace_id
          AND active.data_mode = op.data_mode
          AND active.protocol_version >= 2
          AND active.status = 'running'
          AND active.lease_expires_at > clock_timestamp()
      ) < ns.max_worker_concurrency
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = op.namespace_id
          AND grant_row.data_mode = op.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? op.operation_type
          AND EXISTS (
            SELECT 1
            FROM public.backend_service_principals AS service_principal
            WHERE service_principal.principal_id = principal
              AND service_principal.status = 'active'
          )
      )
    ORDER BY op.priority DESC, op.available_at, op.created_at, op.operation_id
    FOR UPDATE OF ns, op SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_operations AS op
    SET status = 'running',
        attempt_count = op.attempt_count + 1,
        lease_generation = op.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE op.operation_id = candidate.operation_id
    RETURNING op.operation_id, op.namespace_id, op.data_mode,
      op.namespace_generation, op.run_id, op.arm_id, op.subject_id,
      op.operation_type, candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch, candidate.claim_retrieval_policy_epoch,
      op.lease_generation, op.fencing_token
  )
  SELECT claimed.operation_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.operation_type,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_operation(TEXT, TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_operation(
  target_operation_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid heartbeat lease duration';
  END IF;
  UPDATE public.sleep_domain_operations
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE operation_id = target_operation_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_operation(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_operation(
  target_operation_id TEXT,
  expected_cas_version BIGINT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  requested_outcome_class TEXT,
  requested_next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'succeeded', 'retry', 'failed', 'dead_letter',
    'reconciliation_required', 'outcome_unknown'
  ) OR (requested_final_status = 'retry'
    AND requested_next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid operation final status';
  END IF;
  UPDATE public.sleep_domain_operations
  SET status = requested_final_status,
      outcome_class = requested_outcome_class,
      available_at = COALESCE(requested_next_available_at, available_at),
      cas_version = cas_version + 1,
      lease_expires_at = NULL,
      lease_owner = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp(),
      dead_lettered_at = CASE WHEN requested_final_status = 'dead_letter'
        THEN clock_timestamp() ELSE dead_lettered_at END
  WHERE operation_id = target_operation_id
    AND status = 'running'
    AND cas_version = expected_cas_version
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp()
    AND COALESCE(
      authorization_snapshot_json,
      workload_authorization_snapshot_json
    ) ->> 'authorization_epoch' = NULLIF(
      current_setting('sleepagent.authorization_epoch', TRUE), ''
    )
    AND COALESCE(
      authorization_snapshot_json,
      workload_authorization_snapshot_json
    ) ->> 'privacy_epoch' = NULLIF(
      current_setting('sleepagent.privacy_epoch', TRUE), ''
    )
    AND COALESCE(
      authorization_snapshot_json,
      workload_authorization_snapshot_json
    ) ->> 'retrieval_policy_epoch' = NULLIF(
      current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_operation(
  TEXT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_operation_fence_allows(
  target_operation_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.sleep_domain_operations AS op
    WHERE op.operation_id = target_operation_id
      AND op.status = 'running'
      AND op.lease_generation = expected_lease_generation
      AND op.fencing_token = expected_fencing_token
      AND op.worker_instance = NULLIF(
        current_setting('sleepagent.worker_instance', TRUE), ''
      )
      AND public.sleepagent_principal_context_allows()
      AND public.sleepagent_subject_generation_scope_allows(
        op.namespace_id, op.data_mode, op.subject_id,
        op.namespace_generation, op.run_id, op.arm_id
      )
      AND op.lease_expires_at > clock_timestamp()
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'authorization_epoch' = NULLIF(
        current_setting('sleepagent.authorization_epoch', TRUE), ''
      )
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'privacy_epoch' = NULLIF(
        current_setting('sleepagent.privacy_epoch', TRUE), ''
      )
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'retrieval_policy_epoch' = NULLIF(
        current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
      )
  )
$$;

REVOKE ALL ON FUNCTION sleepagent_operation_fence_allows(
  TEXT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_enforce_scenario_clock_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT public.sleepagent_operation_fence_allows(
    NEW.last_command_operation_id,
    NEW.command_lease_generation,
    NEW.command_fencing_token
  ) THEN
    RAISE EXCEPTION 'ScenarioClock mutation requires a live operation fence';
  END IF;
  IF TG_OP = 'UPDATE'
     AND NEW.clock_version <> OLD.clock_version + 1 THEN
    RAISE EXCEPTION 'ScenarioClock version must advance by one';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_replay_scenario_clock_fence
  ON backend_replay_scenario_clocks;
CREATE TRIGGER backend_replay_scenario_clock_fence
BEFORE INSERT OR UPDATE ON backend_replay_scenario_clocks
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_scenario_clock_fence();

CREATE OR REPLACE FUNCTION sleepagent_claim_delivery(
  requested_destination TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  delivery_intent_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  handler_name TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
BEGIN
  IF requested_destination IS NULL OR requested_destination = ''
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted delivery claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT intent.delivery_intent_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.backend_delivery_intents AS intent
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = intent.namespace_id
     AND namespace_row.data_mode = intent.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = intent.namespace_id
     AND epoch_row.data_mode = intent.data_mode
     AND epoch_row.subject_id = intent.subject_id
    WHERE intent.data_mode = deployment_mode
      AND intent.destination = requested_destination
      AND (
        intent.status IN ('pending', 'retry')
        OR (intent.status IN ('running', 'dispatching')
          AND intent.lease_expires_at <= clock_timestamp())
      )
      AND intent.available_at <= clock_timestamp()
      AND intent.attempt_count < intent.max_attempts
      AND namespace_row.status = 'active'
      AND intent.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND intent.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND intent.authorization_snapshot_json ->>
        'retrieval_policy_epoch' = epoch_row.retrieval_policy_epoch::text
      AND (
        intent.predecessor_sequence IS NULL
        OR EXISTS (
          SELECT 1
          FROM public.backend_delivery_intents AS predecessor
          WHERE predecessor.destination = intent.destination
            AND predecessor.aggregate_type = intent.aggregate_type
            AND predecessor.aggregate_id = intent.aggregate_id
            AND predecessor.aggregate_sequence =
              intent.predecessor_sequence
            AND predecessor.status = 'delivered'
        )
      )
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = intent.namespace_id
          AND grant_row.data_mode = intent.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? intent.handler_name
      )
    ORDER BY intent.priority DESC, intent.available_at,
      intent.created_at, intent.delivery_intent_id
    FOR UPDATE OF namespace_row, intent SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_delivery_intents AS intent
    SET status = 'running',
        attempt_count = intent.attempt_count + 1,
        lease_generation = intent.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        dispatch_permit_at = NULL,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE intent.delivery_intent_id = candidate.delivery_intent_id
    RETURNING intent.delivery_intent_id, intent.namespace_id,
      intent.data_mode, intent.namespace_generation, intent.run_id,
      intent.arm_id, intent.subject_id, intent.handler_name,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      intent.lease_generation, intent.fencing_token
  )
  SELECT claimed.delivery_intent_id, claimed.namespace_id,
    claimed.data_mode, claimed.namespace_generation, claimed.run_id,
    claimed.arm_id, claimed.subject_id, claimed.handler_name,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_delivery(TEXT, TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_delivery(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid delivery heartbeat duration';
  END IF;
  UPDATE public.backend_delivery_intents
  SET lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status IN ('running', 'dispatching')
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_delivery(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_mark_delivery_dispatching(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  UPDATE public.backend_delivery_intents
  SET status = 'dispatching',
      dispatch_permit_at = clock_timestamp(),
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_mark_delivery_dispatching(
  TEXT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_delivery(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'delivered', 'retry', 'dead_letter', 'outcome_unknown'
  ) OR (requested_final_status = 'retry' AND next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid delivery final status';
  END IF;
  UPDATE public.backend_delivery_intents
  SET status = requested_final_status,
      available_at = COALESCE(next_available_at, available_at),
      delivered_at = CASE WHEN requested_final_status = 'delivered'
        THEN clock_timestamp() ELSE delivered_at END,
      lease_expires_at = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status IN ('running', 'dispatching')
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_delivery(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_consume_pending_handle(
  target_handle_id TEXT,
  expected_cas_version BIGINT,
  expected_target_state_version BIGINT,
  expected_target_sha256 TEXT,
  expected_policy_sha256 TEXT,
  command_receipt_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  UPDATE public.backend_pending_handles
  SET status = 'consumed',
      cas_version = cas_version + 1,
      consumed_at = clock_timestamp(),
      consumed_by_command_receipt_id = command_receipt_id
  WHERE handle_id = target_handle_id
    AND status = 'pending'
    AND cas_version = expected_cas_version
    AND target_state_version = expected_target_state_version
    AND target_sha256 = expected_target_sha256
    AND policy_sha256 = expected_policy_sha256
    AND actor_id = NULLIF(
      current_setting('sleepagent.actor_id', TRUE), ''
    )
    AND role = NULLIF(current_setting('sleepagent.actor_role', TRUE), '')
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND authorization_epoch::text = NULLIF(
      current_setting('sleepagent.authorization_epoch', TRUE), ''
    )
    AND privacy_epoch::text = NULLIF(
      current_setting('sleepagent.privacy_epoch', TRUE), ''
    )
    AND retrieval_policy_epoch::text = NULLIF(
      current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
    )
    AND expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_consume_pending_handle(
  TEXT, BIGINT, BIGINT, TEXT, TEXT, TEXT
) FROM PUBLIC;

-- New work-protocol state is subject scoped after claim.
DROP POLICY IF EXISTS sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox;
CREATE POLICY sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox
  USING (
    (scope_protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (scope_protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (scope_protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (scope_protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

ALTER TABLE sleep_domain_normalization_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_normalization_work FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_normalization_work_scope
  ON sleep_domain_normalization_work
  USING (
    (protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

DROP POLICY IF EXISTS sleep_domain_operation_scope ON sleep_domain_operations;
CREATE POLICY sleep_domain_operation_scope ON sleep_domain_operations
  USING (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

DROP POLICY IF EXISTS sleep_domain_domain_outbox_scope
  ON sleep_domain_domain_outbox;
CREATE POLICY sleep_domain_domain_outbox_scope ON sleep_domain_domain_outbox
  USING (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

ALTER TABLE backend_command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_command_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_command_receipt_scope ON backend_command_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_invocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_invocations FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_invocation_scope ON backend_invocations
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_invocation_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_invocation_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_invocation_journal_scope ON backend_invocation_journal
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_operation_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_operation_heartbeats FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_operation_heartbeat_scope
  ON backend_operation_heartbeats
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_operation_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_operation_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_operation_checkpoint_scope
  ON backend_operation_checkpoints
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_delivery_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_delivery_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_delivery_intent_scope ON backend_delivery_intents
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_delivery_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_delivery_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_delivery_journal_scope ON backend_delivery_journal
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_consumer_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_consumer_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_consumer_inbox_scope ON backend_consumer_inbox
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_consumer_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_consumer_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_consumer_checkpoint_scope ON backend_consumer_checkpoints
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

DROP POLICY IF EXISTS sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views;
CREATE POLICY sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views
  USING (
    (
      (protocol_version < 2 AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
      OR (protocol_version >= 2
        AND sleepagent_subject_generation_scope_allows(
          namespace_id, data_mode, subject_id, namespace_generation,
          run_id, arm_id
        ))
    )
    AND (
      sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR role = sleepagent_scope_setting('sleepagent.actor_role')
    )
  )
  WITH CHECK (
    (
      (protocol_version < 2 AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
      OR (protocol_version >= 2
        AND sleepagent_subject_generation_scope_allows(
          namespace_id, data_mode, subject_id, namespace_generation,
          run_id, arm_id
        ))
    )
    AND sleepagent_scope_setting('sleepagent.process_role') = 'worker'
  );

ALTER TABLE backend_product_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_product_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_product_attempt_scope ON backend_product_attempts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_pending_handles ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_pending_handles FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_pending_handle_scope ON backend_pending_handles
  USING (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR (
        actor_id = sleepagent_scope_setting('sleepagent.actor_id')
        AND role = sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  )
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR (
        actor_id = sleepagent_scope_setting('sleepagent.actor_id')
        AND role = sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  );

ALTER TABLE backend_replay_scenario_clocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_scenario_clocks FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_replay_scenario_clock_scope
  ON backend_replay_scenario_clocks
  USING (sleepagent_namespace_generation_scope_allows(
    namespace_id, data_mode, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_namespace_generation_scope_allows(
    namespace_id, data_mode, namespace_generation, run_id, arm_id
  ));

CREATE OR REPLACE FUNCTION sleepagent_demo_seed_access_allows()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_id = sleepagent_scope_setting(
        'sleepagent.service_principal_id'
      )
      AND principal.database_role_name::text = session_user::text
      AND principal.status = 'active'
      AND principal.principal_kind IN ('demo_controller', 'worker')
  )
$$;

ALTER TABLE backend_demo_seed_allowlist ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_seed_allowlist FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_seed_allowlist_access
  ON backend_demo_seed_allowlist
  USING (sleepagent_demo_seed_access_allows())
  WITH CHECK (sleepagent_demo_seed_access_allows());

ALTER TABLE backend_demo_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_traces FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_trace_scope ON backend_demo_traces
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));
