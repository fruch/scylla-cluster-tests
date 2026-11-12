FROM eclipse-temurin:21 as builder

ARG BRANCH
ARG REPO

ENV BRANCH=${BRANCH:-master}
ENV REPO=${REPO:-https://github.com/scylladb/YCSB.git}

RUN apt-get update && apt-get install -y \
    git \
    maven

RUN git clone ${REPO} -b ${BRANCH}

RUN cd YCSB; mvn -pl dynamodb -am clean package -DskipTests
RUN cd /YCSB/dynamodb/target && mkdir -p YCSB && tar xvvf ycsb-dynamo*.tar.gz -C YCSB --strip-components 1

RUN cd YCSB; mvn -pl scylla -am clean package -DskipTests
RUN cd /YCSB/scylla/target && mkdir -p YCSB && tar xvvf ycsb-scylla-*.tar.gz -C YCSB --strip-components 1


# openjdk:8 was removed from Docker Hub; temurin 8 on jammy still ships python2.
FROM eclipse-temurin:8-jdk-jammy AS app

RUN apt-get update && apt-get install -y python2
RUN ln -s /usr/bin/python2 /usr/bin/python

RUN echo 'networkaddress.cache.ttl=0' >> $JAVA_HOME/jre/lib/security/java.security
RUN echo 'networkaddress.cache.negative.ttl=0' >> $JAVA_HOME/jre/lib/security/java.security
COPY java.policy $JAVA_HOME/jre/lib/security/java.policy
COPY --from=builder /YCSB/dynamodb/target/YCSB /YCSB
COPY --from=builder /YCSB/scylla/target/YCSB /YCSB
