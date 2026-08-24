# Minimal three-tier demo stack: ALB -> web servers -> Postgres.
#
# The security-group rules define exactly one integration chain:
#   internet -> ALB:443 -> web:80 -> db:5432
#
# This is a demo fixture, not production code. It intentionally stays small.

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.tags, { Name = "itest-demo-vpc" })
}

resource "aws_subnet" "a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]

  tags = merge(local.tags, { Name = "itest-demo-subnet-a" })
}

resource "aws_subnet" "b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = data.aws_availability_zones.available.names[1]

  tags = merge(local.tags, { Name = "itest-demo-subnet-b" })
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

# ALB: accepts HTTPS from the public internet (inline ingress rule).
resource "aws_security_group" "alb" {
  name        = "itest-demo-alb"
  description = "Allow HTTPS from the internet to the load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from anywhere"
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

  tags = merge(local.tags, { Name = "itest-demo-alb-sg" })
}

# Web tier: accepts HTTP from the ALB security group only.
resource "aws_security_group" "web" {
  name        = "itest-demo-web"
  description = "Allow HTTP from the ALB to the web servers"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "itest-demo-web-sg" })
}

# Standalone rule: web:80 from the ALB SG (SG-to-SG reference).
resource "aws_security_group_rule" "web_from_alb" {
  type                     = "ingress"
  description              = "HTTP from the ALB"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  security_group_id        = aws_security_group.web.id
  source_security_group_id = aws_security_group.alb.id
}

# Database tier: accepts Postgres from the web security group only.
resource "aws_security_group" "db" {
  name        = "itest-demo-db"
  description = "Allow Postgres from the web tier to the database"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "itest-demo-db-sg" })
}

# Standalone rule: db:5432 from the web SG (SG-to-SG reference).
resource "aws_security_group_rule" "db_from_web" {
  type                     = "ingress"
  description              = "Postgres from the web tier"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.web.id
}

# ---------------------------------------------------------------------------
# Compute + load balancer + database
# ---------------------------------------------------------------------------

resource "aws_lb" "app" {
  name               = "itest-demo-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [aws_subnet.a.id, aws_subnet.b.id]

  tags = local.tags
}

resource "aws_instance" "web" {
  count                  = 2
  ami                    = "ami-0c02fb55956c7d316"
  instance_type          = "t3.micro"
  subnet_id              = element([aws_subnet.a.id, aws_subnet.b.id], count.index)
  vpc_security_group_ids = [aws_security_group.web.id]

  tags = merge(local.tags, { Name = "itest-demo-web-${count.index}" })
}

resource "aws_db_instance" "postgres" {
  identifier             = "itest-demo-db"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  db_name                = "app"
  username               = "app"
  password               = var.db_password
  vpc_security_group_ids = [aws_security_group.db.id]
  skip_final_snapshot    = true

  tags = local.tags
}
