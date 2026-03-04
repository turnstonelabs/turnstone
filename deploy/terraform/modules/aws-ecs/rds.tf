# ---------- Random Password ----------

resource "random_password" "db" {
  length  = 32
  special = false
}

# ---------- DB Subnet Group ----------

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-${var.environment}"
  subnet_ids = var.private_subnet_ids
  tags       = local.common_tags
}

# ---------- RDS PostgreSQL ----------

resource "aws_db_instance" "this" {
  identifier = "${var.name_prefix}-${var.environment}"

  engine               = "postgres"
  engine_version       = "17"
  instance_class       = var.db_instance_class
  allocated_storage    = 20
  storage_type         = "gp3"
  storage_encrypted    = true
  deletion_protection  = true
  skip_final_snapshot  = false
  final_snapshot_identifier = "${var.name_prefix}-${var.environment}-final"

  db_name  = "turnstone"
  username = "turnstone"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  backup_retention_period = 7
  multi_az                = false

  tags = local.common_tags
}
