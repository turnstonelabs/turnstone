output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer."
  value       = aws_lb.this.dns_name
}

output "server_url" {
  description = "HTTP URL for the Turnstone server API and web UI."
  value       = "http://${aws_lb.this.dns_name}"
}

output "console_url" {
  description = "HTTP URL for the Turnstone console dashboard."
  value       = "http://${aws_lb.this.dns_name}:8090"
}

output "cluster_arn" {
  description = "ARN of the ECS cluster."
  value       = aws_ecs_cluster.this.arn
}

output "rds_endpoint" {
  description = "Endpoint of the RDS PostgreSQL instance (host:port)."
  value       = aws_db_instance.this.endpoint
}

output "redis_endpoint" {
  description = "Primary endpoint of the ElastiCache Redis replication group."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}
