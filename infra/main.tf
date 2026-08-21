# infra/main.tf — Terraform configuration for AWS ECS Fargate, RDS Postgres & ALB

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# VPC & Subnets (Default/Existing or Data source)
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Security Groups
resource "aws_security_group" "alb_sg" {
  name        = "${var.app_name}-alb-sg"
  description = "ALB Security Group"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ecs_sg" {
  name        = "${var.app_name}-ecs-sg"
  description = "ECS Task Security Group"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds_sg" {
  name        = "${var.app_name}-rds-sg"
  description = "RDS Security Group"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_sg.id]
  }
}

# RDS Postgres Instance
resource "aws_db_instance" "postgres" {
  identifier             = "${var.app_name}-db"
  allocated_storage      = 20
  engine                 = "postgres"
  engine_version         = "15.4"
  instance_class         = "db.t4g.micro"
  db_name                = "hiero_db"
  username               = "hiero_admin"
  password               = var.db_password
  vpc_security_group_ids = [aws_security_group.rds_sg.id]
  skip_final_snapshot    = true
}

# ECS Cluster & Fargate Task
resource "aws_ecs_cluster" "cluster" {
  name = "${var.app_name}-cluster"
}

resource "aws_ecs_task_definition" "task" {
  family                   = "${var.app_name}-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"

  container_definitions = jsonencode([
    {
      name      = var.app_name
      image     = var.container_image
      essential = true
      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
        }
      ]
      environment = [
        { name = "ENVIRONMENT", value = "production" },
        { name = "DATABASE_URL", value = "postgresql+asyncpg://hiero_admin:${var.db_password}@${aws_db_instance.postgres.endpoint}/hiero_db" }
      ]
    }
  ])
}

resource "aws_ecs_service" "service" {
  name            = "${var.app_name}-service"
  cluster         = aws_ecs_cluster.cluster.id
  task_definition = aws_ecs_task_definition.task.arn
  launch_type     = "FARGATE"
  desired_count   = 1

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.ecs_sg.id]
    assign_public_ip = true
  }
}
