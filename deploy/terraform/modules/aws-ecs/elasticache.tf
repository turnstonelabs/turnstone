# ---------- ElastiCache Subnet Group ----------

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-${var.environment}"
  subnet_ids = var.private_subnet_ids
  tags       = local.common_tags
}

# ---------- ElastiCache Redis Replication Group ----------

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${var.name_prefix}-${var.environment}"
  description          = "Turnstone Redis for MQ and session state"

  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_clusters   = 1
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  automatic_failover_enabled = false

  tags = local.common_tags
}
