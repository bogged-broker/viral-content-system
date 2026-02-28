# Complete Directory Tree

This document contains every single file and folder in the project.

```
viralcontentsystem/
├── .env
├── CODEBASE_COMPLETE_REPORT.md
├── Dockerfile
├── ORCHESTRATION_GUIDE.md
├── README.md
├── RUN_SYSTEM_GUIDE.md
├── account_system
│   ├── __pycache__
│   │   ├── account_health_monitor.cpython-38.pyc
│   │   ├── geo_allocator.cpython-38.pyc
│   │   ├── identity_router.cpython-38.pyc
│   │   └── verification_manager.cpython-38.pyc
│   ├── account_health_monitor.py
│   ├── account_invariants.py
│   ├── account_profile.py
│   ├── behavior_fingerprint.py
│   ├── enforcement_monitor.py
│   ├── geo_allocator.py
│   ├── identity_router.py
│   ├── network_affiliation.py
│   ├── reputation_ledger.py
│   ├── risk_signal_extractor.py
│   ├── suppression_tracker.py
│   ├── trust_decay.py
│   ├── trust_registry.py
│   ├── trust_scoring.py
│   ├── verification_manager.py
│   └── watchdog.py
├── canonical_trend_identity.py
├── checkpoints
│   └── checkpoints.db
├── config
│   ├── __init__.py
│   ├── __pycache__
│   │   ├── __init__.cpython-311.pyc
│   │   ├── __init__.cpython-38.pyc
│   │   ├── config_errors.cpython-311.pyc
│   │   ├── config_errors.cpython-38.pyc
│   │   ├── config_hashing.cpython-38.pyc
│   │   ├── config_loader.cpython-311.pyc
│   │   ├── config_loader.cpython-38.pyc
│   │   ├── config_policy.cpython-38.pyc
│   │   ├── config_resolver.cpython-38.pyc
│   │   ├── config_types.cpython-311.pyc
│   │   ├── config_types.cpython-38.pyc
│   │   ├── config_versioning.cpython-311.pyc
│   │   ├── defaults.cpython-311.pyc
│   │   ├── defaults.cpython-38.pyc
│   │   ├── deployment_profile.cpython-311.pyc
│   │   └── deployment_profile.cpython-38.pyc
│   ├── config_errors.py
│   ├── config_hashing.py
│   ├── config_loader.py
│   ├── config_policy.py
│   ├── config_resolver.py
│   ├── config_schema.py
│   ├── config_types.py
│   ├── config_versioning.py
│   ├── defaults.py
│   ├── deployment_profile.py
│   ├── environments
│   │   ├── __init__.py
│   │   ├── development.yaml
│   │   ├── production.yaml
│   │   └── staging.yaml
│   ├── runtime_infra.py
│   ├── runtime_secrets.py
│   └── versions.json
├── data
│   ├── __init__.py
│   ├── __pycache__
│   │   ├── __init__.cpython-311.pyc
│   │   └── __init__.cpython-38.pyc
│   ├── lineage
│   │   ├── TIER0_10_10_ENHANCEMENTS.md
│   │   ├── TIER0_HARDENING.md
│   │   ├── __pycache__
│   │   │   ├── compatibility_matrix.cpython-38.pyc
│   │   │   ├── invariants.cpython-38.pyc
│   │   │   ├── lineage_merkle.cpython-38.pyc
│   │   │   ├── migration_executor.cpython-38.pyc
│   │   │   ├── migration_orchestrator.cpython-38.pyc
│   │   │   ├── migration_plan.cpython-38.pyc
│   │   │   ├── migration_snapshot.cpython-38.pyc
│   │   │   ├── schema_versions.cpython-38.pyc
│   │   │   └── version_validator.cpython-38.pyc
│   │   ├── audit_hooks.py
│   │   ├── canonical_encoding.py
│   │   ├── compatibility_matrix.py
│   │   ├── deterministic_sandbox.py
│   │   ├── distributed_consensus_adapter.py
│   │   ├── formal_lineage_model.md
│   │   ├── invariants.py
│   │   ├── lineage_auditor.py
│   │   ├── lineage_governance_lock.py
│   │   ├── lineage_graph.py
│   │   ├── lineage_merkle.py
│   │   ├── lineage_record.py
│   │   ├── lineage_registry.py
│   │   ├── lineage_store.py
│   │   ├── lineage_types.py
│   │   ├── linearizable_append_contract.py
│   │   ├── migration_executor.py
│   │   ├── migration_orchestrator.py
│   │   ├── migration_plan.py
│   │   ├── migration_snapshot.py
│   │   ├── purity_analysis.py
│   │   ├── replay_guard.py
│   │   ├── schema_versions.py
│   │   └── version_validator.py
│   ├── pipelines
│   │   ├── README.MD
│   │   ├── aggregation
│   │   │   ├── __pycache__
│   │   │   │   ├── aggregation_context.cpython-38.pyc
│   │   │   │   ├── aggregation_runner.cpython-38.pyc
│   │   │   │   └── counters.cpython-38.pyc
│   │   │   ├── aggregation_context.py
│   │   │   ├── aggregation_errors.py
│   │   │   ├── aggregation_invariants.py
│   │   │   ├── aggregation_runner.py
│   │   │   ├── counters.py
│   │   │   ├── reducers copy.py
│   │   │   ├── reducers.py
│   │   │   ├── runner_phases.py
│   │   │   └── windows.py
│   │   ├── base
│   │   │   ├── __pycache__
│   │   │   │   └── pipeline_step.cpython-38.pyc
│   │   │   ├── pipeline_context.py
│   │   │   ├── pipeline_invariants.py
│   │   │   ├── pipeline_runner.py
│   │   │   └── pipeline_step.py
│   │   ├── computation
│   │   │   ├── __init__.py
│   │   │   ├── __pycache__
│   │   │   │   └── computation_spec.cpython-38.pyc
│   │   │   ├── computation_context.py
│   │   │   ├── computation_errors.py
│   │   │   ├── computation_executor.py
│   │   │   ├── computation_hashing.py
│   │   │   ├── computation_invariants.py
│   │   │   ├── computation_registry.py
│   │   │   ├── computation_spec.py
│   │   │   └── computation_spec_errors.py
│   │   ├── ingestion
│   │   │   ├── __init__.py
│   │   │   ├── __pycache__
│   │   │   │   ├── __init__.cpython-311.pyc
│   │   │   │   └── content_ingest.cpython-311.pyc
│   │   │   ├── account_ingest copy.py
│   │   │   ├── account_ingest.py
│   │   │   ├── base
│   │   │   │   ├── __init__.py
│   │   │   │   ├── __pycache__
│   │   │   │   │   ├── __init__.cpython-311.pyc
│   │   │   │   │   ├── ingest_context.cpython-311.pyc
│   │   │   │   │   └── ingest_result.cpython-311.pyc
│   │   │   │   ├── ingest_context.py
│   │   │   │   ├── ingest_errors.py
│   │   │   │   ├── ingest_invaraints.py
│   │   │   │   ├── ingest_result.py
│   │   │   │   └── ingest_utils.py
│   │   │   ├── builders
│   │   │   │   ├── __init__.py
│   │   │   │   └── result_factory.py
│   │   │   ├── content_ingest copy 2.py
│   │   │   ├── content_ingest.py
│   │   │   ├── engagement_ingest.py
│   │   │   ├── ingest_registry.py
│   │   │   ├── moderation_ingest copy.py
│   │   │   ├── moderation_ingest.py
│   │   │   └── recovery_ingest.py
│   │   ├── replay
│   │   │   ├── __pycache__
│   │   │   │   ├── replay_io_sandbox.cpython-38.pyc
│   │   │   │   ├── replay_lineage.cpython-38.pyc
│   │   │   │   ├── replay_ordering.cpython-38.pyc
│   │   │   │   └── replay_runner.cpython-38.pyc
│   │   │   ├── replay_context.py
│   │   │   ├── replay_errors.py
│   │   │   ├── replay_invariants.py
│   │   │   ├── replay_io_sandbox.py
│   │   │   ├── replay_lineage.py
│   │   │   ├── replay_ordering.py
│   │   │   ├── replay_plan.py
│   │   │   ├── replay_report.py
│   │   │   ├── replay_results.py
│   │   │   ├── replay_runner.py
│   │   │   └── replay_validation.py
│   │   ├── transforms
│   │   │   ├── __pycache__
│   │   │   │   └── normalization.cpython-311.pyc
│   │   │   ├── deduplication.py
│   │   │   ├── filtering.py
│   │   │   ├── joining.py
│   │   │   ├── normalization.py
│   │   │   ├── transform_invariants.py
│   │   │   └── validation.py
│   │   ├── validation
│   │   │   ├── __init__ copy.py
│   │   │   ├── __init__.py
│   │   │   ├── __pycache__
│   │   │   │   ├── __init__.cpython-38.pyc
│   │   │   │   ├── input_validator.cpython-38.pyc
│   │   │   │   └── output_validator.cpython-38.pyc
│   │   │   ├── input_validator.py
│   │   │   ├── input_validator_idk.py
│   │   │   ├── output_validator.py
│   │   │   └── pipeline_audit.py
│   │   └── windows
│   │       ├── window_errors.py
│   │       ├── window_identity.py
│   │       ├── window_invariants.py
│   │       ├── window_models.py
│   │       ├── window_tests.py
│   │       └── windows.py
│   ├── schemas
│   │   ├── __init__.py
│   │   ├── account.py
│   │   ├── analytics.py
│   │   ├── base.py
│   │   ├── content.py
│   │   ├── engagement.py
│   │   ├── moderation.py
│   │   └── recovery.py
│   ├── validation
│   │   ├── __init__.py
│   │   ├── __pycache__
│   │   │   ├── __init__.cpython-311.pyc
│   │   │   ├── __init__.cpython-38.pyc
│   │   │   ├── ejection_reasons.cpython-311.pyc
│   │   │   ├── ejection_reasons.cpython-38.pyc
│   │   │   ├── error_model.cpython-311.pyc
│   │   │   ├── error_model.cpython-38.pyc
│   │   │   ├── field_rules.cpython-38.pyc
│   │   │   ├── policy_profiles.cpython-311.pyc
│   │   │   ├── policy_profiles.cpython-38.pyc
│   │   │   ├── semantic_rules.cpython-38.pyc
│   │   │   ├── validation_contract.cpython-311.pyc
│   │   │   ├── validation_contract.cpython-38.pyc
│   │   │   ├── validators.cpython-311.pyc
│   │   │   └── validators.cpython-38.pyc
│   │   ├── audit_log_model.py
│   │   ├── compatibility_guards.py
│   │   ├── contracts.py
│   │   ├── ejection_reasons.py
│   │   ├── error_model.py
│   │   ├── field_rules.py
│   │   ├── invariants.py
│   │   ├── policy_profiles.py
│   │   ├── rejection_reasons.py
│   │   ├── semantic_rules.py
│   │   ├── validation_contract.py
│   │   └── validators.py
│   └── versioning
│       ├── __init__.py
│       ├── __pycache__
│       │   └── __init__.cpython-38.pyc
│       ├── model
│       │   ├── __init__.py
│       │   ├── __pycache__
│       │   │   ├── semantic_policy.cpython-38.pyc
│       │   │   └── version.cpython-38.pyc
│       │   ├── semantic_policy.py
│       │   ├── version.py
│       │   ├── version_graph.py
│       │   └── version_range.py
│       └── policy
│           ├── __init__.py
│           ├── __pycache__
│           │   └── compatibility_policy.cpython-38.pyc
│           ├── compatibility_policy.py
│           └── deprecation_policy.py
├── deterministic_velocity_scoring.py
├── evaluation
│   ├── __pycache__
│   │   └── viral_score.cpython-311.pyc
│   ├── ab_testing.py
│   ├── cross_platform_normalizer.py
│   ├── early_signal_detector.py
│   ├── evaluation_invariants.py
│   ├── longitudinal_drift_monitor.py
│   ├── metrics.py
│   ├── suppression_analyzer.py
│   ├── validation_pipeline.py
│   ├── viral_score.py
│   └── virality_calibration.py
├── experiments
│   ├── CONTROL_ASSIGNMENTnew.py
│   ├── __pycache__
│   │   ├── experiment_invariants.cpython-38.pyc
│   │   ├── experiment_manager.cpython-38.pyc
│   │   ├── experiment_runtime.cpython-38.pyc
│   │   └── variant_generator.cpython-38.pyc
│   ├── archival
│   │   ├── replay_loader.py
│   │   └── snapshot_serializer.py
│   ├── confidence_estimator.py
│   ├── control_assignment copy_testinggemini_LOOKATLINE178.py
│   ├── control_assignment.py
│   ├── control_assignmentdifferent.py
│   ├── effect_size_analyzer.py
│   ├── experiment_invariants.py
│   ├── experiment_manager.py
│   ├── experiment_registry.py
│   ├── experiment_runtime.py
│   ├── experiment_spec.py
│   ├── freeze_manager.py
│   ├── hypothesis_engine.py
│   ├── outcome_collector.py
│   ├── postmortem_analyzer.py
│   ├── reports
│   │   ├── experiment_diff.py
│   │   └── experiment_report.py
│   ├── rollback_manager.py
│   ├── rollout_manager.py
│   ├── statistical_tests.py
│   └── variant_generator.py
├── factories
│   ├── __pycache__
│   │   └── factory_manager.cpython-311.pyc
│   ├── anomaly_detector.py
│   ├── budget_allocator.py
│   ├── budget_allocator_complex.py
│   ├── factory_lifecycle.py
│   ├── factory_manager.py
│   ├── factory_metrics.py
│   ├── factory_registry.py
│   ├── niche_router.py
│   ├── scaling_controller.py
│   └── scaling_controller_complex.py
├── feature_extraction
│   ├── __pycache__
│   │   └── virality_feature_engine.cpython-311.pyc
│   ├── cross_model_correlation.py
│   ├── engagement_pattern_learner.py
│   ├── feature_registry.py
│   ├── multimodal_features.py
│   ├── sentiment_analzyer.py
│   └── virality_feature_engine.py
├── generate_tree.py
├── generation
│   ├── __pycache__
│   │   ├── content_pipeline.cpython-311.pyc
│   │   └── script_generator.cpython-311.pyc
│   ├── ai_text_generator.py
│   ├── audio_synthesizer.py
│   ├── content_pipeline.py
│   ├── retention_optimizer.py
│   ├── script_generator.py
│   ├── storyboard.py
│   └── visual_composer.py
├── infra
│   ├── __init__.py
│   ├── __pycache__
│   │   ├── __init__.cpython-311.pyc
│   │   ├── __init__.cpython-38.pyc
│   │   ├── bootstrap.cpython-311.pyc
│   │   ├── clock.cpython-311.pyc
│   │   ├── config_registry.cpython-311.pyc
│   │   ├── feature_flags.cpython-311.pyc
│   │   ├── id_generator.cpython-311.pyc
│   │   └── runtime_context.cpython-311.pyc
│   ├── bootstrap.py
│   ├── clock.py
│   ├── config_registry.py
│   ├── failure_recovery.py
│   ├── feature_flags.py
│   ├── id_generator.py
│   ├── idempotency
│   │   ├── __init__.py
│   │   └── event_identity_store.py
│   ├── limits
│   │   ├── backpressure.py
│   │   ├── quota_manager.py
│   │   └── rate_limiter.py
│   ├── logging
│   │   ├── audit_logger.py
│   │   ├── log_schemas.py
│   │   ├── log_sinks.py
│   │   └── structured_logger.py
│   ├── observability
│   │   ├── anomaly_detector.py
│   │   ├── grafana_dashboards
│   │   │   ├── pipeline_metrics.json
│   │   │   └── system_overview.json
│   │   ├── health_checks.py
│   │   ├── health_endpoint.py
│   │   ├── health_policy.py
│   │   ├── metric_registry.py
│   │   ├── metrics_collector.py
│   │   ├── prometheus.yml
│   │   ├── trace_query.py
│   │   ├── tracing.py
│   │   └── watchdog_hooks.py
│   ├── persistence
│   │   ├── __init__.py
│   │   ├── __pycache__
│   │   │   ├── __init__.cpython-311.pyc
│   │   │   ├── __init__.cpython-38.pyc
│   │   │   ├── lock_manager.cpython-311.pyc
│   │   │   ├── lock_manager.cpython-38.pyc
│   │   │   ├── serialization.cpython-38.pyc
│   │   │   ├── snapshot_store.cpython-311.pyc
│   │   │   ├── state_migrator.cpython-311.pyc
│   │   │   ├── state_serializer.cpython-311.pyc
│   │   │   ├── state_serializer.cpython-38.pyc
│   │   │   └── transactional_store.cpython-38.pyc
│   │   ├── backend
│   │   │   ├── __init__.py
│   │   │   ├── __pycache__
│   │   │   │   └── persistence_errors.cpython-38.pyc
│   │   │   ├── adapters
│   │   │   │   ├── __init__.py
│   │   │   │   ├── filesystem_adapter.py
│   │   │   │   └── memory_adapter.py
│   │   │   ├── backend_base.py
│   │   │   ├── backend_config_schemas.py
│   │   │   ├── backend_factory.py
│   │   │   ├── backend_factory_errors.py
│   │   │   ├── backend_policy.py
│   │   │   ├── backend_validators.py
│   │   │   ├── base.py
│   │   │   ├── filesystem_backend.py
│   │   │   ├── integrity_guard.py
│   │   │   ├── key_namespace.py
│   │   │   ├── kv_backend.py
│   │   │   ├── logical_clock.py
│   │   │   ├── memory.py
│   │   │   ├── memory_backend.py
│   │   │   ├── object_store_backend.py
│   │   │   ├── persistence_errors.py
│   │   │   ├── postgres_backend.py
│   │   │   ├── redis_backend.py
│   │   │   ├── transaction_invariants.py
│   │   │   ├── transactional_backend.py
│   │   │   └── transactional_store.py
│   │   ├── integrity_guard.py
│   │   ├── lock_manager.py
│   │   ├── serialization.py
│   │   ├── snapshot_store.py
│   │   ├── state_backend.py
│   │   ├── state_migrator.py
│   │   ├── state_serializer.py
│   │   └── transactional_store.py
│   ├── recovery
│   │   ├── README.md
│   │   ├── __pycache__
│   │   │   └── recovery_orchestrator.cpython-38.pyc
│   │   ├── audit
│   │   │   ├── __init__ copy.py
│   │   │   ├── __init__.py
│   │   │   ├── __pycache__
│   │   │   │   └── recovery_log.cpython-38.pyc
│   │   │   ├── audit_chain.py
│   │   │   ├── audit_events.py
│   │   │   ├── audit_export.py
│   │   │   ├── audit_invariants.py
│   │   │   ├── audit_logger.py
│   │   │   ├── audit_models.py
│   │   │   ├── audit_query.py
│   │   │   ├── audit_redactor.py
│   │   │   ├── audit_validator.py
│   │   │   ├── audit_validator_idk.py
│   │   │   ├── audit_verifier.py
│   │   │   ├── recovery_log.py
│   │   │   ├── recovery_summary.py
│   │   │   └── redaction_policy.py
│   │   ├── checkpoints
│   │   │   ├── __init__.py
│   │   │   ├── checkpoint_index.py
│   │   │   ├── checkpoint_models.py
│   │   │   ├── checkpoint_resolver.py
│   │   │   ├── checkpoint_retention.py
│   │   │   └── checkpoint_validator.py
│   │   ├── damage_assessor.py
│   │   ├── failure_recovery.py
│   │   ├── recovery_checkpoint_invariants.py
│   │   ├── recovery_corruption_detection.py
│   │   ├── recovery_dependency_graph.py
│   │   ├── recovery_invariants.py
│   │   ├── recovery_models.py
│   │   ├── recovery_orchestrator.py
│   │   ├── recovery_resume_boundary.py
│   │   ├── recovery_validation.py
│   │   ├── repair_strategies.py
│   │   ├── rollback_executer.py
│   │   └── workflows
│   │       ├── __init__.py
│   │       ├── repair_strategies
│   │       │   ├── __init__.py
│   │       │   ├── __pycache__
│   │       │   │   ├── __init__.cpython-38.pyc
│   │       │   │   ├── checkpoint_rollback.cpython-38.pyc
│   │       │   │   ├── full_replay.cpython-38.pyc
│   │       │   │   ├── hash_verification_repair.cpython-38.pyc
│   │       │   │   ├── incremental_rebuild.cpython-38.pyc
│   │       │   │   └── subgraph_repair.cpython-38.pyc
│   │       │   ├── artifact_repair.py
│   │       │   ├── base.py
│   │       │   ├── checkpoint_rollback.py
│   │       │   ├── data_repair.py
│   │       │   ├── edge_repair.py
│   │       │   ├── full_replay.py
│   │       │   ├── hash_verification_repair.py
│   │       │   ├── incremental_rebuild.py
│   │       │   ├── metadata_repair.py
│   │       │   ├── node_repair.py
│   │       │   ├── strategy_invariants.py
│   │       │   └── subgraph_repair.py
│   │       ├── workflow_merge.py
│   │       ├── workflow_models.py
│   │       ├── workflow_repair.py
│   │       ├── workflow_replay.py
│   │       └── workflow_validator.py
│   ├── replay
│   │   ├── input_recorder.py
│   │   └── replay_context.py
│   ├── runtime_context.py
│   ├── safety
│   │   ├── emergency_stop.py
│   │   ├── invariant_engine.py
│   │   └── safety_events.py
│   └── secret_resolver.py
├── ingestion
│   ├── __pycache__
│   │   └── ingestion_pipeline.cpython-311.pyc
│   ├── audio_extractor.py
│   ├── audio_extractor_simple.py
│   ├── ingestion_pipeline.py
│   ├── ingestion_pipeline_complex.py
│   ├── metadata_parser.py
│   ├── metadata_parser_complex.py
│   ├── platform_scrapers
│   │   ├── __pycache__
│   │   │   └── youtube_scraper.cpython-311.pyc
│   │   ├── instagram_scraper.py
│   │   ├── instagram_scraper_complete.py
│   │   ├── reddit_scraper.py
│   │   ├── tiktok_scraper.py
│   │   ├── tiktok_scraper_simple.py
│   │   ├── youtube_scraper.py
│   │   └── youtube_scraper_complex.py
│   ├── sentiment_analzyer_COMPLEX.py
│   ├── trend_aggregator.py
│   └── video_downloader.py
├── k8s-deployment.yaml
├── license
├── limits
│   ├── backpressure.py
│   ├── quota_manager.py
│   └── rate_limiter.py
├── logs
│   └── pipeline
├── long_tail_config.yaml
├── long_tail_tracker.py
├── long_tail_tracker_new.py
├── main.py
├── metadata_cli.py
├── metadata_loader.py
├── metadata_monitor.py
├── metadata_runner.py
├── metadata_store.py
├── metric_invariants.py
├── models
│   ├── README.MD
│   ├── ml_models
│   │   ├── content_ranker.py
│   │   ├── emotional_arc_predictor.py
│   │   ├── engagement_predictor.py
│   │   └── style_classifier.py
│   ├── rl_agents
│   │   ├── factory
│   │   │   ├── factory_agent.py
│   │   │   └── replay_buffer.py
│   │   ├── global
│   │   │   └── multi_agent_manager.py
│   │   └── video_micro
│   │       ├── content_agent.py
│   │       ├── environment.py
│   │       ├── policy_network.py
│   │       └── value_network.py
│   └── training
│       ├── checkpoint_manager.py
│       ├── curriculum_learning.py
│       ├── data_gate.py
│       ├── gradient_governor.py
│       ├── optimizer.py
│       └── scheduler.py
├── orchestration
│   ├── __init__.py
│   ├── __pycache__
│   │   ├── __init__.cpython-311.pyc
│   │   ├── __init__.cpython-38.pyc
│   │   ├── dependency_graph.cpython-311.pyc
│   │   ├── execution_graph.cpython-311.pyc
│   │   ├── failure_policy.cpython-311.pyc
│   │   ├── lifecycle_manager.cpython-311.pyc
│   │   ├── resource_governor.cpython-311.pyc
│   │   ├── shutdown_manager.cpython-311.pyc
│   │   ├── startup_sequence.cpython-311.pyc
│   │   ├── system_orchestrator.cpython-311.pyc
│   │   └── system_orchestrator.cpython-38.pyc
│   ├── agent_comms.py
│   ├── dependency_graph.py
│   ├── execution_graph.py
│   ├── factory_scheduler.py
│   ├── failure_policy.py
│   ├── lifecycle_manager.py
│   ├── priority_router.py
│   ├── resource_governor.py
│   ├── shutdown_manager.py
│   ├── startup_sequence.py
│   ├── system_orchestrator.py
│   └── workflow_manager.py
├── platform_config.yaml
├── platforms
│   └── posting
├── posting
│   ├── README.MD
│   ├── __pycache__
│   │   └── post_dispatcher.cpython-311.pyc
│   ├── cadence_memory.py
│   ├── contracts
│   │   └── invariant_violation.schema.json
│   ├── idempotency.py
│   ├── intent_builder.py
│   ├── kill_switches.py
│   ├── logic
│   │   └── risk_evaluator.py
│   ├── monitoring
│   │   ├── anomaly_detector.py
│   │   ├── audit_logger.py
│   │   └── rollout_controller.py
│   ├── platforms
│   │   ├── common
│   │   │   ├── auth_manager.py
│   │   │   ├── base_poster.py
│   │   │   ├── platform_limits.py
│   │   │   ├── platform_session.py
│   │   │   ├── platform_telemetry.py
│   │   │   ├── posting_errors.py
│   │   │   └── upload_contract.py
│   │   ├── instagram_poster.py
│   │   ├── tiktok_poster.py
│   │   └── youtube_poster.py
│   ├── post_dispatcher.py
│   ├── posting_queue.py
│   ├── posting_state_store.py
│   ├── reconciliation.py
│   └── schemas
│       ├── account_state.schema.json
│       ├── error_taxonomy.schema.json
│       ├── platform_policy.schema.json
│       ├── platform_response.schema.json
│       ├── post_intent.schema.json
│       ├── post_result.schema.json
│       ├── posting_state.schema.json
│       └── state_transition.schema.json
├── processed
│   └── metadata
├── production_dynamic_thresholds.py
├── production_trend_radar.py
├── production_virality_gate.py
├── requirements.txt
├── run_e2e_verification.py
├── run_pipeline_test.py
├── safety_watchdog.py
├── scripts
│   ├── bootstrap_local.bat
│   ├── bootstrap_local.sh
│   ├── replay_from_snapshot.sh
│   ├── run_e2e.bat
│   ├── run_e2e.sh
│   ├── run_pipeline.bat
│   └── run_pipeline.sh
├── session_health_monitor.py
├── sessions.py
├── setup.py
├── tests
│   ├── __init__.py
│   └── e2e_test_full_pipeline.py
├── training.py
├── training_pipeline.py
├── tree_list.txt
├── tree_output.txt
├── tree_structure.txt
├── trend_aggregator_complex.py
└── utils
    ├── __init__.py
    ├── __pycache__
    │   ├── __init__.cpython-311.pyc
    │   ├── __init__.cpython-38.pyc
    │   ├── env.cpython-311.pyc
    │   ├── env.cpython-38.pyc
    │   ├── errors.cpython-311.pyc
    │   ├── errors.cpython-38.pyc
    │   ├── frozen.cpython-311.pyc
    │   ├── hashing.cpython-311.pyc
    │   ├── math.cpython-38.pyc
    │   ├── serialization.cpython-311.pyc
    │   ├── types.cpython-311.pyc
    │   └── types.cpython-38.pyc
    ├── comparators.py
    ├── env.py
    ├── errors.py
    ├── frozen.py
    ├── guards.py
    ├── hashing.py
    ├── ids.py
    ├── iterators.py
    ├── logging.py
    ├── math.py
    ├── ordering.py
    ├── serialization.py
    ├── time.py
    ├── types.py
    └── validation.py
```
