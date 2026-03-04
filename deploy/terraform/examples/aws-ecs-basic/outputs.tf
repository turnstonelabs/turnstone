output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer."
  value       = module.turnstone.alb_dns_name
}

output "server_url" {
  description = "HTTP URL for the Turnstone server."
  value       = module.turnstone.server_url
}

output "console_url" {
  description = "HTTP URL for the Turnstone console."
  value       = module.turnstone.console_url
}

output "cluster_arn" {
  description = "ARN of the ECS cluster."
  value       = module.turnstone.cluster_arn
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint."
  value       = module.turnstone.rds_endpoint
}

output "redis_endpoint" {
  description = "ElastiCache Redis endpoint."
  value       = module.turnstone.redis_endpoint
}
